# -*- coding: utf-8 -*-
"""AI とつなぐ: 画面と AI 側のエンジンの橋渡し（`bridge.py`・bridge.json）。

- 画面が起動したエンジン（GLISS_CLIENT=app）は許可に左右されない
- AI 側（それ以外）: 編集が不許可なら編集のツールは ok=false（Gliss の設定で許可できる旨）。
  保存・書き出しは既定で不許可（bridge.json が無くても）。一時ファイル（render_preview など）は対象外
- 引数しだいのもの: close_project(discard=True) は編集、render_region(path=…) などは保存・書き出し
- AI 側の編集の author は必ず "ai"（apply_plan の既定 human や、author="human" を渡しても）
- load_project() を引数なしで呼ぶと、画面で開いている曲（と編集中のトラック）を開く。無ければ分かるエラー
- どのツールも「編集」「保存・書き出し」「許可なし」のどれかに決めてある（新しいツールの入れ忘れを拾う）
"""
import json
import os
import shutil

import pytest

from conftest import GUIDE, TAKE, needs_clips


def _write_bridge(path, **kw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(version=1, **kw), f, ensure_ascii=False)


@pytest.fixture
def ai(tmp_path, monkeypatch):
    """AI 側のプロセスとして動かす（GLISS_CLIENT を外す）。bridge.json は一時フォルダ。"""
    bpath = str(tmp_path / "Gliss" / "bridge.json")
    monkeypatch.delenv("GLISS_CLIENT", raising=False)
    monkeypatch.setenv("GLISS_BRIDGE", bpath)
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_tracks as mt
    from vocal_engine import mcp_document as md
    yield bpath, m, mt, md
    m._state.update(project=None, session=None, track=None, document=None)
    m._invalidate_renderer()


def _as_app(monkeypatch):
    monkeypatch.setenv("GLISS_CLIENT", "app")


def _as_ai(monkeypatch):
    monkeypatch.delenv("GLISS_CLIENT", raising=False)


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _media(tmp_path):
    d = tmp_path / "song" / "Media"
    d.mkdir(parents=True, exist_ok=True)
    take, guide = str(d / "take.wav"), str(d / "guide.wav")
    shutil.copy(TAKE, take)
    shutil.copy(GUIDE, guide)
    return take, guide


# ---------------------------------------------------------------- 決まり（ファイルを開かない）
def test_bridge_path_and_defaults(tmp_path, monkeypatch):
    from vocal_engine import bridge
    monkeypatch.delenv("GLISS_BRIDGE", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert bridge.path() == os.path.join(str(tmp_path / "Roaming"), "Gliss", "bridge.json")
    # 無い・壊れている: 既定（編集は許す・保存は許さない）
    assert bridge.allow() == {"edit": True, "save": False}
    p = str(tmp_path / "b.json")
    monkeypatch.setenv("GLISS_BRIDGE", p)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{壊れている")
    assert bridge.read() == {} and bridge.allow() == {"edit": True, "save": False}
    _write_bridge(p, allow={"save": True})
    assert bridge.allow() == {"edit": True, "save": True}
    assert bridge.project() is None


def test_category_by_args():
    from vocal_engine import bridge
    assert bridge.category("shift_pitch") == "edit"
    assert bridge.category("undo") == "edit"
    assert bridge.category("save_project") == "save"
    assert bridge.category("export_wav") == "save"
    assert bridge.category("render_preview") is None                    # 一時ファイル
    assert bridge.category("render_preview", {"name": "a.wav"}) is None  # プロジェクトの renders/ の中
    assert bridge.category("render_preview", {"name": "C:/x/a.wav"}) == "save"
    assert bridge.category("render_region") is None
    assert bridge.category("render_region", {"path": "C:/x/a.wav"}) == "save"
    assert bridge.category("export_view_data", {"path": "C:/x/v.json"}) == "save"
    assert bridge.category("close_project") is None
    assert bridge.category("close_project", {"discard": True}) == "edit"
    assert bridge.category("prepare_asr_model") == "save"                 # 数 GB のダウンロード（issue #54）
    assert bridge.category("transcribe") is None and bridge.category("asr_status") is None
    for name in ("list_notes", "analyze_take", "load_project", "open_project", "get_job", "plan_edit"):
        assert bridge.category(name) is None


def test_every_tool_is_classified():
    """ツールを足したら bridge.py にも入れること: 引数に author がある（= 曲を変える）ツールは編集、
    それ以外は、ここに並べた「許可なしで呼べる」か保存・書き出し・引数しだいのどれか。"""
    import inspect
    from vocal_engine import bridge
    from vocal_engine import mcp_server as m
    free = {
        # 開く・作る・閉じる（AI が曲を開くのに要る。作業場所を作るだけでユーザーのファイルは書かない）
        "open_project", "new_project", "load_project", "project_status",
        # 読む・測る・計画だけ・一時ファイル・画面向けの描画データ・ジョブ
        "get_lyrics", "list_utterances", "inspect_lyrics_score", "analyze_take", "get_pitch", "list_notes",
        "list_deviations", "get_phonemes", "plan_edit", "list_connections", "render_audition", "render_view",
        "remeasure", "list_changes", "get_job", "cancel_job", "engine_info",
            # ピッチ検出の方式を選ぶ（そのエンジンの既定。曲は変えない）
            "set_f0_estimator",
            # 裏の準備（issue #63）: 状態を見る・一時停止（曲は変えない）
            "prep_status", "pause_prep",
        "list_tracks", "select_track", "track_overview", "render_tracks",
        # 聞き取り（issue #54）: 候補を返すだけで確定の歌詞は変えない・使えるかを見るだけ
        "transcribe", "asr_status",
        # DAW の Gliss の文書を見る・選ぶ・やめる（ara_relay.py。選んだ後のツールは DAW の Gliss の許可に従う）
        "ara_documents", "ara_attach", "ara_detach",
    }
    names = [f.__name__ for f in m.TOOLS]
    assert len(names) == len(set(names))
    for f in m.TOOLS:
        n = f.__name__
        groups = [n in bridge.EDIT_TOOLS, n in bridge.SAVE_TOOLS, n in bridge.CONDITIONAL_TOOLS, n in free,
                  n in bridge.ARA_TOOLS]
        assert sum(groups) == 1, "%s は bridge.py のどこにも（か 2 か所に）入っている" % n
        if "author" in inspect.signature(f).parameters and n not in ("open_project", "new_project", "load_project"):
            assert n in bridge.EDIT_TOOLS, n
    assert bridge.EDIT_TOOLS <= set(names) and bridge.SAVE_TOOLS <= set(names)
    assert bridge.CONDITIONAL_TOOLS <= set(names) and bridge.ARA_TOOLS <= set(names)


# ---------------------------------------------------------------- 許可（AI 側のプロセス）
@needs_clips
def test_ai_permissions(tmp_path, monkeypatch, ai):
    bpath, m, mt, md = ai
    take, guide = _media(tmp_path)
    _ok(md.new_project(take_path=take))
    # 編集: 既定（bridge.json が無い）は許す
    r = _ok(mt.set_tempo(bpm=120))
    assert r["history"]["undo"]["author"] == "ai"
    # 編集を許さない
    _write_bridge(bpath, allow={"edit": False, "save": False})
    r = mt.set_tempo(bpm=100)
    assert r["ok"] is False and r["permission"] == "edit" and r["tool"] == "set_tempo"
    assert "AI とつなぐ" in r["error"] and "編集" in r["error"]
    for call in (lambda: m.undo(), lambda: mt.add_track(guide), lambda: md.close_project(discard=True)):
        r = call()
        assert r["ok"] is False and r["permission"] == "edit"
    # 読むだけ・閉じるだけ（捨てない）・一時ファイルは許可なしで呼べる
    _ok(mt.list_tracks())
    _ok(md.project_status())
    # 保存・書き出し: 既定で許さない。path があれば名前を付けて保存でも同じ
    path = str(tmp_path / "song" / "曲.gliss")
    r = md.save_project(path)
    assert r["ok"] is False and r["permission"] == "save" and "保存・書き出し" in r["error"]
    assert not os.path.exists(path)
    r = m.export_wav(path=str(tmp_path / "x.wav"))
    assert r["ok"] is False and r["permission"] == "save"
    # 画面が許せば通る
    _write_bridge(bpath, allow={"edit": True, "save": True})
    r = _ok(md.save_project(path))
    assert os.path.exists(path) and r["document"]["dirty"] is False
    _ok(mt.set_tempo(bpm=100))
    # 画面のプロセスは bridge.json に左右されない
    _write_bridge(bpath, allow={"edit": False, "save": False})
    _as_app(monkeypatch)
    _ok(mt.set_tempo(bpm=110))
    _ok(md.save_project())


def test_ai_asr_model_download_needs_save(tmp_path, monkeypatch, ai):
    """聞き取り（issue #54）: 使えるかを見る・聞き取るのは許可なし。モデルの取得は「保存・書き出し」。"""
    bpath, m, mt, md = ai
    from vocal_engine import mcp_asr as ma
    from vocal_engine.asr import recognize
    cfg = tmp_path / "fake-asr.json"
    cfg.write_text(json.dumps({"text": "さくら", "delay_sec": 0.0, "download_sec": 0.0,
                               "file_size": 1000}), encoding="utf-8")
    root = tmp_path / "asr-models"
    monkeypatch.setenv("GLISS_ASR_FAKE", str(cfg))
    monkeypatch.setenv("GLISS_ASR_MODELS_DIR", str(root))
    recognize.set_recognizer_factory(None)
    try:
        st = _ok(ma.asr_status())
        assert st["installed"] is False
        r = ma.transcribe(0.0, 1.0)                           # 曲を開いていない: 許可ではなく通常のエラー
        assert r["ok"] is False and "permission" not in r
        r = ma.prepare_asr_model(background=False)            # 既定（bridge.json が無い）は保存・書き出しを許さない
        assert r["ok"] is False and r["permission"] == "save" and r["tool"] == "prepare_asr_model"
        assert not root.exists()
        _write_bridge(bpath, allow={"edit": True, "save": True})
        r = _ok(ma.prepare_asr_model(background=False))
        assert r["installed"] and os.path.isdir(r["path"])
    finally:
        recognize.set_recognizer_factory(None)


@needs_clips
def test_ai_author_is_forced(tmp_path, monkeypatch, ai):
    bpath, m, mt, md = ai
    take, _g = _media(tmp_path)
    _ok(md.new_project(take_path=take))
    r = _ok(mt.set_tempo(bpm=120, author="human"))           # AI が human を名乗っても ai
    assert r["history"]["undo"]["author"] == "ai"
    _as_app(monkeypatch)
    r = _ok(mt.set_tempo(bpm=121, author="human"))           # 画面は渡したまま
    assert r["history"]["undo"]["author"] == "human"


# ---------------------------------------------------------------- 画面で開いている曲
def test_load_project_without_args_needs_app_project(ai):
    bpath, m, mt, md = ai
    r = md.load_project()
    assert r["ok"] is False and "開いている曲が無い" in r["error"] and bpath in r["error"]
    _write_bridge(bpath, project={"path": os.path.join(os.path.dirname(bpath), "無い.gliss"), "kind": "gliss"})
    r = md.load_project()
    assert r["ok"] is False and "見つからない" in r["error"]


@needs_clips
def test_load_project_without_args_opens_app_song(tmp_path, monkeypatch, ai):
    bpath, m, mt, md = ai
    take, guide = _media(tmp_path)
    # 画面: 2 トラックの曲を保存して、2 本目（ガイドでないボーカル）を編集中にする
    _as_app(monkeypatch)
    _ok(md.new_project(take_path=take))
    t2 = _ok(mt.add_track(guide, select=True))["session"]["current"]
    path = str(tmp_path / "song" / "曲.gliss")
    _ok(md.save_project(path))
    _ok(mt.set_tempo(bpm=123))                                # 保存していない変更（作業場所にだけある）
    work = md.current().work_dir
    _write_bridge(bpath, allow={"edit": True, "save": False},
                  project={"path": path, "kind": "gliss", "name": "曲", "track": t2, "track_name": "guide"})
    # 画面のプロセスの状態を捨てて、AI のプロセスとして開き直す
    m._state.update(project=None, session=None, track=None, document=None)
    _as_ai(monkeypatch)
    r = _ok(md.load_project())
    assert r["from_app"] is True and r["opened"] == "gliss"
    assert r["document"]["path"] == path and r["document"]["work_dir"] == work    # 画面と同じ作業場所
    assert r["session"]["current"] == t2
    assert r["session"]["tempo"]["bpm"] == 123                # 画面の保存していない変更も見える
    # 引数ありは今までどおり
    r = _ok(md.load_project(path))
    assert "from_app" not in r and r["document"]["path"] == path
