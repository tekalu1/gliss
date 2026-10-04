# -*- coding: utf-8 -*-
"""単体の `.gliss` のトラックの編集を、DAW（ARA）の文書へ移すツール（`export_edits` / `import_edits`。
engine/docs/MCP.md §3-4・§3-5）と、アーカイブの F0 の方式（`f0_estimator`）。

音は合成（test_ara_tools.py の `_voice`）。ピッチ検出は重みの要らない Praat。

- `.gliss` → `export_edits` → ARA の文書へ `import_edits` → 再合成が単体の書き出し（`render_region`）とサンプル単位で同じ
- 素材が違えば `mismatch`（何も変えない）・既存の編集があれば `replace` が要る・author が保たれる
- F0 の方式が archive に残り、`ara_restore`・`import_edits` がその方式にする（別の PC で開き直しても音が変わらない）
- ノート ID に頼る編集の対象が無ければ `missing_note_targets`・`mutes` は移らない旨を返す・クリップのトラックはエラー
- 外部の AI の中継（`GLISS_ARA_AI`）: read で断る・edit で通る・外部の変更の番号が進む・編集対象でない修飾にも効く
"""
import copy
import json
import os

import numpy as np
import pytest
import soundfile as sf

from test_ara_tools import Cache, _add, _ok, _open, _voice, _wav


@pytest.fixture
def tr(tmp_path, monkeypatch):
    from vocal_engine import ara_relay as R
    from vocal_engine import mcp_server as m     # 先に読む（mcp_ara などは mcp_server の末尾から読まれる）
    from vocal_engine import mcp_ara as a
    from vocal_engine import mcp_document as md
    from vocal_engine.analysis import f0 as F
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))   # 人の作業場所に作らない
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    monkeypatch.setenv(R.SESSIONS_ENV, str(tmp_path / "sessions"))
    monkeypatch.delenv(R.ALLOW_ENV, raising=False)
    F.set_preferred_estimator("praat")           # 重みが無くても解析できる
    yield m, a, md, R
    R.stop()
    R.detach()
    F.set_preferred_estimator(None)
    md._clear()
    a._reset_render()


def _standalone(m, md, tmp_path, name="take", voice=None):
    """単体のプロジェクトを作り、human・ai の編集（1 つ取り消し）を入れて `.gliss` に保存する。
    (.gliss のパス, 素材の WAV, 編集後の音（単体の書き出し）, 編集後の Project の編集の数)。"""
    from vocal_engine import mcp_tracks as mt
    from vocal_engine.render.region import RegionRenderer, render_region
    src = _wav(tmp_path / "media" / (name + ".wav"), _voice() if voice is None else voice)
    _ok(md.new_project())
    tid = _ok(mt.add_track(src, select=True))["track"]
    _ok(m.analyze_take(background=False))
    notes = [n["id"] for n in _ok(m.list_notes(kind="note"))["notes"]]
    _ok(m.shift_pitch(150, note_id=notes[0], author="human"))
    _ok(m.stretch(1.15, note_id=notes[2], author="ai"))
    _ok(m.shift_pitch(-80, start_sec=3.9, end_sec=4.5, author="human"))
    _ok(m.set_pitch_curve([[0.0, 0], [0.2, 120], [0.4, 0]], start_sec=5.1, end_sec=5.5, author="ai"))
    _ok(m.shift_pitch(40, note_id=notes[4], author="ai"))
    _ok(m.undo())
    p = m._state["project"]
    rr = RegionRenderer.for_project(p, channels="all")
    y, _info = render_region(p, renderer=rr)
    gl = str(tmp_path / "songs" / (name + ".gliss"))
    _ok(md.save_project(gl))
    n_edits = len(p.edits)
    md._clear()
    return gl, src, np.asarray(y, dtype="float32"), n_edits, tid


def _daw_doc(a, tmp_path, src, key="doc-1", ara_id="mod-1"):
    """DAW の文書に、プラグインが書くソースの WAV（float32 の別ファイル）の修飾を 1 つ作る。"""
    x = sf.read(src, dtype="float32")[0]
    daw_src = _wav(tmp_path / "ara-src" / (ara_id + ".wav"), x)
    _open(a, key)
    r = _add(a, ara_id, daw_src, name="テイク")
    return daw_src, r["track"]["id"]


def _sync_all(a, ara_id, daw_src):
    c = Cache(daw_src)
    c.sync(a, ara_id)
    return c


def _same_as(cache, ref):
    ref = ref.reshape(cache.buf.shape)
    d = np.abs(cache.buf.astype("float64") - ref.astype("float64")).max()
    assert np.array_equal(cache.buf, ref), "最大の差 %g" % d


def _set_estimator_in_gliss(path, track_id, est):
    """.gliss のトラックに F0 の方式を書く（方式を記録した版の .gliss の真似）。"""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    t = next(x for x in d["session"]["tracks"] if x["id"] == track_id)
    t["estimator"] = est
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


# ================================================================ 往復（ビット一致）
def test_export_import_renders_the_same_sound_as_the_standalone_export(tr, tmp_path):
    m, a, md, R = tr
    gl, src, ref, n_edits, tid = _standalone(m, md, tmp_path)
    e = _ok(a.export_edits(gl, track=tid, estimator="praat"))
    arc = e["archive"]
    assert arc["format"] == "vocal-editor-archive" and arc["guide"] is None and arc["f0_estimator"] == "praat"
    assert e["estimator"] == "praat" and e["stats"]["edits"] == n_edits and e["stats"]["changesets"] == 5
    assert e["stats"]["authors"]["human"] == 2 and set(e["stats"]["authors"]) == {"human", "ai"}
    assert e["stats"]["undone"] == 1
    assert e["material"]["frames"] == arc["take"]["frames"] and e["material"]["clip_audio_sha256"]
    assert e["not_transferred"] == []
    json.dumps(e)                                  # JSON にそのまま書ける
    # 別のプロセスの DAW の文書: プラグインが書いたソースの WAV（別ファイル・別のバイト）
    daw_src, ara_tid = _daw_doc(a, tmp_path, src)
    r = _ok(a.import_edits(archive=arc))
    assert r["mismatch"] is False and r["imported"] and r["edits"] == n_edits and r["changesets"] == 5
    assert r["authors"] == e["stats"]["authors"] and r["estimator_applied"] == "praat"
    assert r["analysis"] == {"estimator": "praat", "ran": True}
    assert r["missing_note_targets"] == {"count": 0, "ids": []} and r["replaced"] is None
    c = _sync_all(a, "mod-1", daw_src)
    assert not np.array_equal(c.buf, c.orig)
    _same_as(c, ref)
    # author・取り消し済みの changeset が保たれ、list_changes の件数も同じ
    ch = _ok(m.list_changes(include_undone=True))["changesets"]
    assert len(ch) == 5 and {c_["author"] for c_ in ch} == {"human", "ai"}
    assert sum(1 for c_ in ch if c_["undone"]) == 1
    # 取り込んだ編集は Ctrl+Z の列に入らない
    from vocal_engine import mcp_tracks as mt
    assert mt.history_summary()["can_undo"] is False
    # 保存用の写しにも入る（DAW のソングに保存される）
    saved = _ok(a.ara_archive())["archives"]["mod-1"]["archive"]
    assert saved["changesets"] == arc["changesets"] and saved["f0_estimator"] == "praat"


def test_import_edits_reads_the_gliss_directly(tr, tmp_path):
    m, a, md, R = tr
    gl, src, ref, n_edits, tid = _standalone(m, md, tmp_path)
    _set_estimator_in_gliss(gl, tid, "praat")        # 方式を記録した .gliss
    # mutes はセッションの項目で、編集の changeset に入らない（移らない）
    with open(gl, encoding="utf-8") as f:
        d = json.load(f)
    t = next(x for x in d["session"]["tracks"] if x["id"] == tid)
    t["mutes"] = [[1.0, 1.2], [2.0, 2.1]]
    t["gain_db"] = -3.0
    with open(gl, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    e = _ok(a.export_edits(gl, include_archive=False))        # トラックは 1 本なので省ける
    assert "archive" not in e and e["estimator"] == "praat"
    items = {n["item"]: n for n in e["not_transferred"]}
    assert items["mutes"]["count"] == 2 and items["mutes"]["ranges_sec"] == [[1.0, 1.2], [2.0, 2.1]]
    assert items["mix"]["values"] == {"gain_db": -3.0}
    assert any("mutes 2 件は移らない" in w for w in e["warnings"])
    daw_src, _t = _daw_doc(a, tmp_path, src)
    r = _ok(a.import_edits(gliss_path=gl, track=tid))
    assert r["imported"] and r["estimator_applied"] == "praat" and r["edits"] == n_edits
    assert [n["item"] for n in r["not_transferred"]] == ["mutes", "mix"] and r["warnings"]
    _same_as(_sync_all(a, "mod-1", daw_src), ref)
    # archive と gliss_path のどちらか 1 つ
    assert a.import_edits()["ok"] is False
    assert a.import_edits(archive={}, gliss_path=gl)["ok"] is False


# ================================================================ 素材・既存の編集
def test_a_different_material_is_a_mismatch_and_changes_nothing(tr, tmp_path):
    m, a, md, R = tr
    gl, src, ref, n_edits, tid = _standalone(m, md, tmp_path)
    arc = _ok(a.export_edits(gl))["archive"]
    other = _wav(tmp_path / "ara-src" / "other.wav", _voice(transpose=2, seed=3))
    _open(a)
    _add(a, "mod-1", other)
    s = m._state["session"]
    pj = os.path.join(s.project_dir_of(s.find_ara("mod-1")), "project.json")
    before = os.path.exists(pj) and open(pj, "rb").read()
    r = _ok(a.import_edits(archive=arc))
    assert r["mismatch"] is True and r["imported"] is False and "音の中身が違う" in r["reason"]
    assert (open(pj, "rb").read() if os.path.exists(pj) else False) == before
    # 長さが違う
    short = _wav(tmp_path / "ara-src" / "short.wav", _voice()[:44100 * 3])
    _add(a, "mod-2", short)
    s = m._state["session"]
    from vocal_engine import mcp_tracks as mt
    _ok(mt.select_track(s.find_ara("mod-2")["id"]))
    r = _ok(a.import_edits(archive=arc))
    assert r["mismatch"] is True and "長さが違う" in r["reason"]
    assert _ok(a.ara_archive(["mod-2"]))["archives"]["mod-2"]["archive"]["changesets"] == []     # 何も入っていない


def test_existing_edits_need_replace(tr, tmp_path):
    m, a, md, R = tr
    gl, src, ref, n_edits, tid = _standalone(m, md, tmp_path)
    arc = _ok(a.export_edits(gl, estimator="praat"))["archive"]
    daw_src, _t = _daw_doc(a, tmp_path, src)
    _ok(m.analyze_take(background=False))
    notes = [n["id"] for n in _ok(m.list_notes(kind="note"))["notes"]]
    _ok(m.shift_pitch(60, note_id=notes[1], author="human"))          # 画面で手を入れた
    r = a.import_edits(archive=arc)
    assert r["ok"] is False and "replace=true" in r["error"] and "human" in r["error"]
    assert len(m._state["project"].changesets) == 1                   # 何も変えない
    r = _ok(a.import_edits(archive=arc, replace=True))
    assert r["imported"] and r["replaced"] == {"edits": 1, "changesets": 1, "authors": {"human": 1}}
    assert r["edits"] == n_edits and len(_ok(m.list_changes(include_undone=True))["changesets"]) == 5
    _same_as(_sync_all(a, "mod-1", daw_src), ref)
    # 同じ編集をもう一度入れるのは replace が要らない（何も失わない）
    r = _ok(a.import_edits(archive=arc))
    assert r["imported"] and r["replaced"] is None and r["edits"] == n_edits


def test_export_edits_errors_and_empty_track(tr, tmp_path):
    m, a, md, R = tr
    from vocal_engine import mcp_tracks as mt
    src = _wav(tmp_path / "media" / "x.wav", _voice())
    _ok(md.new_project())
    t1 = _ok(mt.add_track(src, select=True))["track"]
    src2 = _wav(tmp_path / "media" / "x2.wav", _voice(transpose=1, seed=5))
    t2 = _ok(mt.add_track(src2))["track"]
    _ok(mt.select_track(t2))
    _ok(mt.select_track(t1))                             # 開いたトラックだけ .gliss に編集（project.json）が入る
    gl = str(tmp_path / "songs" / "x.gliss")
    _ok(md.save_project(gl))
    md._clear()
    r = a.export_edits(gl)                              # ボーカルが 2 本: 指定が要る
    assert r["ok"] is False and "トラックを指定" in r["error"]
    r = _ok(a.export_edits(gl, track=t1))               # 編集の無いトラックは空の archive
    assert r["archive"]["changesets"] == [] and r["stats"]["edits"] == 0 and r["estimator"] is None
    assert any("記録が無い" in w for w in r["warnings"])
    assert a.export_edits(gl, track="nope")["ok"] is False
    assert a.export_edits(gl, track=t1, estimator="nope")["ok"] is False
    assert a.export_edits(gl, track=t1, estimator="auto")["ok"] is False        # 方式として保存できるのは rmvpe・gliss・praat
    assert a.export_edits(str(tmp_path / "none.gliss"))["ok"] is False
    # クリップ（素材の一部）のトラックは移せない
    with open(gl, encoding="utf-8") as f:
        d = json.load(f)
    t = next(x for x in d["session"]["tracks"] if x["id"] == t2)
    pj = d["projects"][t["project_dir"]]
    pj["take"]["offset_frames"] = 44100
    pj["take"]["frames"] = pj["take"]["source_frames"] - 44100
    with open(gl, "w", encoding="utf-8") as f:
        json.dump(d, f)
    r = a.export_edits(gl, track=t2)
    assert r["ok"] is False and "クリップ" in r["error"]
    # 音声が見つからない
    os.remove(src)
    r = a.export_edits(gl, track=t1)
    assert r["ok"] is False and "見つからない" in r["error"]


def test_import_needs_a_selected_modification(tr, tmp_path):
    m, a, md, R = tr
    gl, src, ref, n_edits, tid = _standalone(m, md, tmp_path)
    arc = _ok(a.export_edits(gl))["archive"]
    _open(a)                                             # 修飾が無い
    assert "修飾が選ばれていない" in a.import_edits(archive=arc)["error"]
    md._clear()
    _ok(md.load_project(gl))                             # 単体の曲
    assert "DAW のドキュメントが開かれていない" in a.import_edits(archive=arc)["error"]


# ================================================================ F0 の方式（archive の f0_estimator）
def test_estimator_is_saved_in_the_archive_and_used_when_reopened(tr, tmp_path, monkeypatch):
    m, a, md, R = tr
    from vocal_engine.analysis import f0 as F
    gl, src, ref, n_edits, tid = _standalone(m, md, tmp_path)
    _set_estimator_in_gliss(gl, tid, "praat")
    daw_src, ara_tid = _daw_doc(a, tmp_path, src)
    _ok(a.import_edits(gliss_path=gl))
    s = m._state["session"]
    assert s.estimator_of(s.find_ara("mod-1")) == "praat"
    c1 = _sync_all(a, "mod-1", daw_src)
    _same_as(c1, ref)
    saved = _ok(a.ara_archive(["mod-1"]))["archives"]["mod-1"]["archive"]
    assert saved["f0_estimator"] == "praat"
    blob = json.dumps(saved)                             # DAW のソングに保存される（プラグインは不透明な JSON として持つ）

    # 別の PC: 作業場所が違い、選んでいる方式は別（praat 以外）。archive の方式で解析する
    md._clear()
    a._reset_render()
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work2"))
    F.set_preferred_estimator("gliss")
    daw_src2 = _wav(tmp_path / "ara-src2" / "mod-1.wav", sf.read(daw_src, dtype="float32")[0])
    _open(a, "doc-1")
    _add(a, "mod-1", daw_src2, name="テイク")
    r = _ok(a.ara_restore("mod-1", json.loads(blob)))
    assert r["mismatch"] is False and r["estimator_applied"] == "praat" and r["estimator_note"] is None
    s = m._state["session"]
    assert s.estimator_of(s.find_ara("mod-1")) == "praat"
    assert m._state["project"].f0_estimator() == "praat"        # 選んでいる方式（gliss）より明示が先
    _ok(m.analyze_take(background=False))                       # 既定の呼び出し = トラックの方式
    assert m._state["project"].analysis["take"]["estimator"] == "praat"
    c2 = _sync_all(a, "mod-1", daw_src2)
    assert np.array_equal(c1.buf, c2.buf)
    # 保存し直しても方式が消えない（解析の前でも・後でも）
    assert _ok(a.ara_archive(["mod-1"]))["archives"]["mod-1"]["archive"]["f0_estimator"] == "praat"


def test_archive_without_a_recorded_estimator_keeps_the_analysed_one(tr, tmp_path):
    """to_archive は、明示した方式が無ければ前に解析した方式を持つ（未解析なら None）。"""
    m, a, md, R = tr
    from vocal_engine import mcp_tracks as mt
    src = _wav(tmp_path / "media" / "y.wav", _voice())
    _ok(md.new_project())
    _ok(mt.add_track(src, select=True))
    assert m._state["project"].to_archive()["f0_estimator"] is None
    _ok(m.analyze_take(background=False))
    assert m._state["project"].to_archive()["f0_estimator"] == "praat"


def test_unusable_estimator_is_not_applied(tr, tmp_path, monkeypatch):
    m, a, md, R = tr
    from vocal_engine.analysis import f0 as F
    gl, src, ref, n_edits, tid = _standalone(m, md, tmp_path)
    arc = _ok(a.export_edits(gl, estimator="praat"))["archive"]
    monkeypatch.setattr(F, "rmvpe_available", lambda: False)
    daw_src, _t = _daw_doc(a, tmp_path, src)
    r = _ok(a.ara_restore("mod-1", dict(arc, f0_estimator="rmvpe")))
    assert r["mismatch"] is False and r["estimator_applied"] is None and "rmvpe の重み" in r["estimator_note"]
    s = m._state["session"]
    assert s.estimator_of(s.find_ara("mod-1")) is None
    r = _ok(a.ara_restore("mod-1", dict(arc, f0_estimator="bogus")))
    assert r["estimator_applied"] is None and "知らない" in r["estimator_note"]
    r = _ok(a.import_edits(archive=arc, estimator="rmvpe", replace=True))        # 指定しても使えなければ当てない
    assert r["imported"] and r["estimator_applied"] is None and r["analysis"]["ran"] is False
    assert any("rmvpe の重み" in w for w in r["warnings"])


def test_missing_note_targets_are_reported(tr, tmp_path):
    m, a, md, R = tr
    gl, src, ref, n_edits, tid = _standalone(m, md, tmp_path)
    arc = copy.deepcopy(_ok(a.export_edits(gl, estimator="praat"))["archive"])
    assert arc["changesets"][0]["ops"][0]["edit"]["target"]["type"] == "note"
    arc["changesets"][0]["ops"][0]["edit"]["target"]["note_id"] = "n999"     # この解析には無いノート
    daw_src, _t = _daw_doc(a, tmp_path, src)
    r = _ok(a.import_edits(archive=arc))
    assert r["missing_note_targets"] == {"count": 1, "ids": ["n999"]}
    assert any("対象のノートが無い" in w for w in r["warnings"])
    # 方式の記録の無い archive は方式を指定する（記録が無ければ解析をここではしない）
    no_est = dict(arc, f0_estimator=None)
    r = _ok(a.import_edits(archive=no_est, replace=True))
    assert r["estimator"] is None and r["estimator_applied"] is None
    r = _ok(a.import_edits(archive=no_est, estimator="praat", replace=True))
    assert r["estimator_applied"] == "praat"


# ================================================================ 外部の AI の中継
def test_import_through_the_relay(tr, tmp_path, monkeypatch):
    m, a, md, R = tr
    from vocal_engine import mcp_tracks as mt
    monkeypatch.setenv("GLISS_CLIENT", "ara")
    gl, src, ref, n_edits, tid = _standalone(m, md, tmp_path)
    _set_estimator_in_gliss(gl, tid, "praat")
    arc = _ok(a.export_edits(gl))["archive"]
    daw_src, ta = _daw_doc(a, tmp_path, src, ara_id="mod-A")
    daw_src_b = _wav(tmp_path / "ara-src" / "mod-B.wav", sf.read(src, dtype="float32")[0])
    tb = _add(a, "mod-B", daw_src_b, name="テイク B")["track"]["id"]
    assert mt.current_track_id() == ta                   # プラグインの画面は mod-A
    R.attach(ara_id="mod-B")
    seq = _ok(a.ara_revs())["external"]["seq"]
    # 読むだけの許可: 取り込みは断る（変えない）
    monkeypatch.setenv(R.ALLOW_ENV, "read")
    r = R.forward("import_edits", {"archive": arc})
    assert r["ok"] is False and r["permission"] == "edit" and "GLISS_ARA_AI" in r["error"]
    assert _ok(a.ara_revs())["external"]["seq"] == seq
    # export_edits は転送せず AI のエンジンで答える（LOCAL_TOOLS）。ara_ で始まらない名前なので断られない
    assert "export_edits" in R.LOCAL_TOOLS and "import_edits" not in R.LOCAL_TOOLS
    assert R.denied("import_edits", {}) is None or R.level() == "read"
    monkeypatch.setenv(R.ALLOW_ENV, "edit")
    rev0 = _ok(a.ara_revs())["revs"]["mod-B"]
    r = R.forward("import_edits", {"archive": arc})
    assert r["ok"] and r["imported"] and r["edits"] == n_edits and r["ara"]["ara_id"] == "mod-B", r
    # author は中継が "ai" に上書きしない（archive のまま human が残る）
    assert r["authors"]["human"] == 2 and set(r["authors"]) == {"human", "ai"}
    ext = _ok(a.ara_revs())
    assert ext["external"]["seq"] == seq + 1 and ext["external"]["track_id"] == tb
    assert ext["revs"]["mod-B"] != rev0 and mt.current_track_id() == ta
    s = m._state["session"]
    pa, _ = mt._track_project(s, s.track(ta))
    assert len(pa.edits) == 0                            # プラグインの画面の修飾は変わらない
    assert {c.author for c in mt._track_project(s, s.track(tb))[0].changesets} == {"human", "ai"}
    _same_as(_sync_all(a, "mod-B", daw_src_b), ref)
    # gliss_path を直接渡す（プラグインのエンジンが .gliss を読む）
    r = R.forward("import_edits", {"gliss_path": gl, "track": tid, "replace": True})
    assert r["ok"] and r["imported"] and r["estimator_applied"] == "praat", r
    # 画面の編集対象に取り込む（編集対象の Project は古いまま残らない）
    R.forward("select_track", {"track_id": ta})
    r = R.forward("import_edits", {"archive": arc})
    assert r["ok"] and r["imported"], r
    assert len(m._state["project"].changesets) == 5 and len(m._state["project"].edits) == n_edits
    _same_as(_sync_all(a, "mod-A", daw_src), ref)


def test_external_engine_forwards_import_and_exports_locally(tr, tmp_path, monkeypatch):
    """外部の AI のエンジン（GLISS_CLIENT なし）: ara_attach の後、import_edits は転送され、export_edits は自分で読む。"""
    m, a, md, R = tr
    import threading
    from vocal_engine import bridge
    monkeypatch.setenv("GLISS_CLIENT", "ara")
    gl, src, ref, n_edits, tid = _standalone(m, md, tmp_path)
    daw_src, ta = _daw_doc(a, tmp_path, src)
    real = bridge.client
    me = threading.get_ident()
    monkeypatch.setattr(bridge, "client", lambda: "" if threading.get_ident() == me else real())
    monkeypatch.setenv("GLISS_BRIDGE", str(tmp_path / "no-bridge.json"))
    assert not bridge.is_app()
    _ok(a.ara_attach(ara_id="mod-1"))
    e = _ok(a.export_edits(gl, track=tid, estimator="praat"))        # 転送されない（ara キーが付かない）
    assert "ara" not in e and e["stats"]["edits"] == n_edits
    r = a.import_edits(archive=e["archive"])
    assert r["ok"] and r["imported"] and r["ara"]["ara_id"] == "mod-1", r
    _ok(a.ara_detach())
    monkeypatch.setattr(bridge, "client", real)          # ここからプラグインのエンジンとして
    _same_as(_sync_all(a, "mod-1", daw_src), ref)
