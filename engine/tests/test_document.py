# -*- coding: utf-8 -*-
"""issue #33: 新規プロジェクト・開く・保存（`project/document.py`・`mcp_document.py`）。

- 新規（無題）→ トラックを足す → 名前を付けて保存（.gliss）。作業場所はファイルのものに移り、無題の作業場所は消える
- 未保存の判定: 編集で true、保存で false。編集対象の切り替え・解析では変わらない
- 閉じる: 保存しない（discard）で最後に保存した中身に戻る。捨てずに閉じて開き直すと続きから（recovered）
- 音声のパス: .gliss と音声を一緒に別の場所へ移しても開ける（相対パス）。見つからなければ missing
- 取り消しの履歴も保存される（開き直して Ctrl+Z・外したトラックも編集ごと戻る）
- 旧形式（projects/…）: そのまま開ける・未保存にならない・名前を付けて保存すると moved_to.json、
  同じテイクを開き直すと .gliss を開く。new_project(take_path) は旧形式があればそちらを開く
- ファイルが外で書き換わっていて作業場所にも保存していない変更があったら、作業場所を backup/ に写してから読む
"""
import json
import os
import shutil

import pytest

from conftest import CLIP_E, GUIDE, TAKE, needs_clips

pytestmark = [needs_clips]


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_tracks as mt
    from vocal_engine import mcp_document as md
    yield m, mt, md
    m._state.update(project=None, session=None, track=None, document=None)
    m._invalidate_renderer()


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _media(tmp_path, sub="Media"):
    d = tmp_path / "song" / sub
    d.mkdir(parents=True, exist_ok=True)
    take = str(d / "take.wav")
    guide = str(d / "guide.wav")
    shutil.copy(TAKE, take)
    shutil.copy(GUIDE, guide)
    return take, guide


def _first_note(m):
    return [n for n in m._state["project"].take_notes if n.kind == "note"][0].id


def test_new_add_save_as_and_dirty(tmp_path, mcp):
    m, mt, md = mcp
    take, guide = _media(tmp_path)
    r = _ok(md.new_project())
    assert r["document"]["kind"] == "untitled" and r["document"]["dirty"] is False
    assert r["session"]["tracks"] == [] and r["project_dir"] is None
    untitled = r["document"]["work_dir"]
    assert untitled.startswith(str(tmp_path / "work"))
    r = _ok(mt.add_track(take, select=True))
    _ok(mt.add_track(guide, guide=True))
    assert _ok(md.project_status())["document"]["dirty"] is True        # 無題にトラックがある
    # 保存先が無い保存はできない
    assert md.save_project()["ok"] is False
    path = str(tmp_path / "song" / "曲.gliss")
    r = _ok(md.save_project(path))
    assert r["saved"] == path and r["moved"] is True
    assert r["document"] == dict(r["document"], kind="gliss", path=path, name="曲", dirty=False)
    assert not os.path.exists(untitled)                                   # 無題の作業場所は消える
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    assert d["format"] == "gliss-project" and d["version"] == 1 and d["name"] == "曲"
    tr = d["session"]["tracks"]
    assert [t["rel"] for t in tr] == ["Media/take.wav", "Media/guide.wav"]
    assert all(os.path.isabs(t["path"]) for t in tr)
    assert "analysis" not in json.dumps(list(d["projects"].values()))    # 解析の要約は入れない
    # 自動推定の歌詞は保存対象なので、初回解析後は未保存になる。
    _ok(m.analyze_take())
    t1, t2 = [t["id"] for t in r["session"]["tracks"]]
    _ok(mt.select_track(t2))
    _ok(mt.select_track(t1))
    assert _ok(md.project_status())["document"]["dirty"] is True
    _ok(md.save_project())
    assert _ok(md.project_status())["document"]["dirty"] is False
    # 編集で未保存、保存で戻る
    _ok(m.shift_pitch(30.0, note_id=_first_note(m), author="human"))
    assert _ok(md.project_status())["document"]["dirty"] is True
    r = _ok(md.save_project())
    assert r["moved"] is False and r["document"]["dirty"] is False
    # ミュート／ソロは曲に保存されるもの（DAW と同じく未保存になる）
    _ok(mt.set_track(t2, mute=True))
    assert _ok(md.project_status())["document"]["dirty"] is True


def test_close_discard_and_recover(tmp_path, mcp):
    m, mt, md = mcp
    take, guide = _media(tmp_path)
    _ok(md.new_project(take_path=take))
    path = str(tmp_path / "song" / "a.gliss")
    _ok(md.save_project(path))
    nid = _first_note(m)
    _ok(m.shift_pitch(40.0, note_id=nid, author="human"))
    # 捨てずに閉じる（落ちたのと同じ）→ 開き直すと続きから
    _ok(md.close_project())
    r = _ok(md.load_project(path))
    assert r["recovered"] is True and r["document"]["dirty"] is True and r["edits"] == 1
    # 保存しないで閉じる → 最後に保存した中身
    _ok(md.close_project(discard=True))
    r = _ok(md.load_project(path))
    assert r["recovered"] is False and r["document"]["dirty"] is False and r["edits"] == 0
    # 無題を保存しないで閉じると作業場所ごと消える
    r = _ok(md.new_project(take_path=take))
    wd = r["document"]["work_dir"]
    assert os.path.isdir(wd)
    _ok(md.close_project(discard=True))
    assert not os.path.exists(wd)
    assert _ok(md.project_status())["document"] is None


def test_load_warns_about_unsaved_and_fresh_discards(tmp_path, mcp):
    """同じ作業場所で開き直すと、保存していない編集が乗ったまま（recovered）。warnings で知らせ、fresh で捨てる。"""
    m, mt, md = mcp
    take, guide = _media(tmp_path)
    _ok(md.new_project(take_path=take))
    path = str(tmp_path / "song" / "f.gliss")
    _ok(md.save_project(path))
    nid = _first_note(m)
    _ok(m.shift_pitch(40.0, note_id=nid, author="human"))
    r = _ok(md.load_project(path))                       # 閉じずにもう一度開く（AI のスクリプトの流れ）
    assert r["recovered"] is True and r["edits"] == 1
    assert any("fresh=true" in w for w in r["warnings"])
    r = _ok(md.load_project(path, fresh=True))
    assert r["recovered"] is False and r["discarded_unsaved"] is True and r["edits"] == 0
    assert r["document"]["dirty"] is False


def test_open_project_goes_under_the_work_dir(tmp_path, monkeypatch):
    """VOCAL_ENGINE_PROJECTS を渡さず作業場所を渡したら、旧形式のプロジェクトも作業場所の下に作る。"""
    from vocal_engine.project import store
    monkeypatch.delenv("VOCAL_ENGINE_PROJECTS", raising=False)
    monkeypatch.setattr(store, "PROJECTS_ROOT", store._DEFAULT_PROJECTS_ROOT)
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "w"))
    assert store.projects_root() == os.path.join(str(tmp_path / "w"), "projects")
    monkeypatch.setenv("VOCAL_ENGINE_PROJECTS", str(tmp_path / "p"))
    monkeypatch.setattr(store, "PROJECTS_ROOT", str(tmp_path / "p"))
    assert store.projects_root() == str(tmp_path / "p")


def test_history_survives_save_and_moved_media(tmp_path, mcp):
    """取り消しの履歴ごと保存される。.gliss と音声を一緒に移しても開ける（相対パス）。"""
    m, mt, md = mcp
    take, guide = _media(tmp_path)
    r = _ok(md.new_project(take_path=take, guide_path=guide))
    t1, t2 = [t["id"] for t in r["session"]["tracks"]]
    assert r["session"]["guide"] == t2
    nid = _first_note(m)
    _ok(m.shift_pitch(25.0, note_id=nid, author="human"))
    _ok(mt.select_track(t2))
    _ok(mt.remove_track(t1))                                  # 編集のあるトラックを外す
    path = str(tmp_path / "song" / "b.gliss")
    _ok(md.save_project(path))
    _ok(md.close_project())
    # 曲のフォルダごと別の場所へ（元は消す）
    moved = tmp_path / "moved"
    shutil.move(str(tmp_path / "song"), str(moved))
    r = _ok(md.load_project(str(moved / "b.gliss")))
    assert r["missing"] == []
    paths = [t["path"] for t in r["session"]["tracks"]]
    assert paths == [str(moved / "Media" / "guide.wav")]
    # Ctrl+Z: 外したのが戻る（編集ごと・移した先の音声で）
    u = _ok(m.undo())
    assert u["undone"]["label"] == "トラックを外す"
    ts = _ok(mt.list_tracks())["tracks"]
    assert [t["path"] for t in ts] == [str(moved / "Media" / "take.wav"), str(moved / "Media" / "guide.wav")]
    _ok(mt.select_track(t1))
    assert len(m._state["project"].edits) == 1
    u = _ok(m.undo())
    assert u["undone"]["label"] == "ピッチ" and len(m._state["project"].edits) == 0
    # 保存し直すと絶対パスも移した先になる。音声が見つからなければ missing（トラックはそのまま）
    _ok(md.save_project())
    with open(str(moved / "b.gliss"), encoding="utf-8") as f:
        assert json.load(f)["session"]["tracks"][0]["path"] == str(moved / "Media" / "take.wav")
    _ok(md.close_project())
    os.remove(str(moved / "Media" / "guide.wav"))
    shutil.copy(str(moved / "b.gliss"), str(tmp_path / "c.gliss"))
    r = _ok(md.load_project(str(tmp_path / "c.gliss")))
    assert len(r["missing"]) == 1 and r["missing"][0].endswith("guide.wav")


def test_legacy_project_opens_and_save_as_redirects(tmp_path, mcp, monkeypatch):
    m, mt, md = mcp
    from vocal_engine.project import store
    monkeypatch.setattr(store, "PROJECTS_ROOT", str(tmp_path / "projects"))
    take, guide = _media(tmp_path)
    # 旧形式で開いて編集する（issue #33 より前の動き。projects/<名前>-<sha8>/）
    r = _ok(m.open_project(take, guide))
    legacy = r["project_dir"]
    assert legacy.startswith(str(tmp_path / "projects"))
    assert r["document"]["kind"] == "legacy" and r["document"]["dirty"] is False
    _ok(m.shift_pitch(20.0, note_id=_first_note(m), author="human"))
    assert _ok(md.project_status())["document"]["dirty"] is False       # 自動で保存している
    _ok(md.close_project())
    # ディレクトリを開く（session.json でも同じ）
    r = _ok(md.load_project(os.path.join(legacy, "session.json")))
    assert r["opened"] == "legacy" and r["edits"] == 1 and r["document"]["work_dir"] == legacy
    # 新規（テイクから）でも、旧形式があればそちらを開く
    _ok(md.close_project())
    r = _ok(md.new_project(take_path=take))
    assert r["opened"] == "legacy" and r["edits"] == 1
    # 名前を付けて保存 → .gliss に移る。旧形式のディレクトリには moved_to.json
    path = str(tmp_path / "song" / "legacy.gliss")
    r = _ok(md.save_project(path))
    assert r["moved"] is True and r["document"]["kind"] == "gliss" and r["edits"] == 1
    with open(os.path.join(legacy, "moved_to.json"), encoding="utf-8") as f:
        assert json.load(f)["gliss"] == path
    # 解析のキャッシュは写している（開き直しで作り直さない）
    assert os.path.exists(os.path.join(r["document"]["work_dir"], "cache", "take-analysis.json")) or \
        not os.path.exists(os.path.join(legacy, "cache", "take-analysis.json"))
    # 同じテイクを旧形式で開き直すと .gliss を開く
    _ok(md.close_project())
    r = _ok(m.open_project(take))
    assert r["opened"] == "gliss" and r["document"]["path"] == path and r["edits"] == 1
    r = _ok(md.load_project(legacy))
    assert r["opened"] == "gliss"


def test_file_changed_outside_backs_up_unsaved_work(tmp_path, mcp):
    m, mt, md = mcp
    take, guide = _media(tmp_path)
    _ok(md.new_project(take_path=take))
    path = str(tmp_path / "song" / "d.gliss")
    r = _ok(md.save_project(path))
    wd = r["document"]["work_dir"]
    snapshot = open(path, encoding="utf-8").read()
    _ok(m.shift_pitch(40.0, note_id=_first_note(m), author="human"))       # 作業場所に未保存の変更
    _ok(md.close_project())
    # ファイルが外で書き換わった（別の PC で保存して同期された、など）
    d = json.loads(snapshot)
    d["session"]["tracks"][0]["name"] = "外で変えた名前"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    r = _ok(md.load_project(path))
    assert r["backup"] and os.path.isdir(r["backup"]) and r["backup"].startswith(wd)
    assert r["session"]["tracks"][0]["name"] == "外で変えた名前" and r["edits"] == 0
    assert r["document"]["dirty"] is False
    # 退避した作業場所には編集が残っている
    with open(os.path.join(r["backup"], "session.json"), encoding="utf-8") as f:
        assert json.load(f)["tracks"][0]["name"] != "外で変えた名前"


def test_same_file_shares_work_dir_between_processes(tmp_path, mcp):
    """同じファイルを開けば同じ作業場所（画面と Claude Code が同じものを触る）。別名で保存したら別の作業場所。"""
    from vocal_engine.project import document as D
    m, mt, md = mcp
    take, guide = _media(tmp_path)
    _ok(md.new_project(take_path=take))
    a = str(tmp_path / "song" / "e.gliss")
    r = _ok(md.save_project(a))
    assert r["document"]["work_dir"] == D.work_dir_for(a)
    b = str(tmp_path / "song" / "e2")                          # 拡張子は付ける
    r = _ok(md.save_project(b))
    assert r["saved"] == b + ".gliss" and r["document"]["work_dir"] == D.work_dir_for(b + ".gliss")
    assert os.path.exists(a) and os.path.exists(b + ".gliss")
    # 開けないもの
    bad = tmp_path / "x.gliss"
    bad.write_text('{"format": "other"}', encoding="utf-8")
    assert md.load_project(str(bad))["ok"] is False
    newer = tmp_path / "y.gliss"
    newer.write_text('{"format": "gliss-project", "version": 99}', encoding="utf-8")
    assert "新しい版" in md.load_project(str(newer))["error"]


def test_tool_list_has_document_tools():
    from vocal_engine import mcp_server as m
    names = [f.__name__ for f in m.TOOLS]
    for n in ("new_project", "load_project", "save_project", "project_status", "close_project"):
        assert n in names
    assert len(names) == len(set(names))


def test_inst_only_project_has_no_current_track(tmp_path, mcp):
    """伴奏だけのプロジェクトも作れて保存できる（編集対象なし）。"""
    m, mt, md = mcp
    inst = str(tmp_path / "Inst_mix.wav")
    shutil.copy(CLIP_E, inst)
    _ok(md.new_project())
    r = _ok(mt.add_track(inst))
    assert r["selected"] is False and mt.current_track_id() is None
    path = str(tmp_path / "inst.gliss")
    _ok(md.save_project(path))
    _ok(md.close_project())
    r = _ok(md.load_project(path))
    assert r["project_dir"] is None and [t["kind"] for t in r["session"]["tracks"]] == ["inst"]


def test_save_over_another_project_does_not_reuse_its_caches(tmp_path, mcp, monkeypatch):
    """同じ .gliss のパスに別のプロジェクトを保存し直しても、前のプロジェクトの解析のキャッシュを使わない。"""
    m, mt, md = mcp
    from vocal_engine.project import store
    monkeypatch.setattr(store, "PROJECTS_ROOT", str(tmp_path / "projects"))
    take, guide = _media(tmp_path)
    other = str(tmp_path / "song" / "Media" / "other.wav")
    shutil.copy(CLIP_E, other)
    path = str(tmp_path / "song" / "x.gliss")
    # 旧形式 A（最初のテイクのプロジェクトは作業場所の直下 "."）を解析して保存
    _ok(m.open_project(take))
    _ok(m.analyze_take())
    _ok(md.save_project(path))
    wd = _ok(md.project_status())["document"]["work_dir"]
    assert os.path.exists(os.path.join(wd, "cache", "take-analysis.json"))
    _ok(md.close_project())
    # 旧形式 B を同じパスに保存し直す（B の最初のテイクも "."）
    _ok(m.open_project(other))
    _ok(m.analyze_take())
    n_b = len(m._state["project"].take_notes)
    dur_b = m._state["project"].duration_sec
    r = _ok(md.save_project(path))
    assert r["document"]["work_dir"] == wd
    _ok(m.analyze_take())
    p = m._state["project"]
    assert p.take["path"] == other and abs(p.duration_sec - dur_b) < 1e-6
    assert len(p.take_notes) == n_b
    assert max(n.end_sec for n in p.take_notes) <= dur_b + 1e-6
