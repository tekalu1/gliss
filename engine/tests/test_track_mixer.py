# -*- coding: utf-8 -*-
"""トラックの音量・パン（`gain_db`・`pan`。見出しのスライダーとノブ。`docs/track-view.md`）。

- 既定値（0 dB・中央）と、set_track / list_tracks の値の丸め
- 取り消しの対象外（M／S と同じ聴き比べの操作）: 履歴に入らず、別の操作を Ctrl+Z / Ctrl+Y しても今の値のまま
- session.json に保存され、読み直しても残る。値の無い古い session.json は既定値で読む
- 再生だけに効く: render_tracks の音のファイルには入らない

合成の WAV だけで回る（素材・重み不要）。
"""
import json
import os

import numpy as np
import pytest
import soundfile as sf


@pytest.fixture
def mcp():
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_tracks as mt
    yield m, mt
    m._state.update(project=None, session=None, track=None)
    m._invalidate_renderer()


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _wav(tmp_path, name, hz):
    path = tmp_path / (name + ".wav")
    sr = 16000
    t = np.arange(sr * 2) / sr
    sf.write(path, 0.3 * np.sin(2 * np.pi * hz * t), sr)
    return str(path)


def _open(m, tmp_path):
    d = str(tmp_path / "mixer")
    r = _ok(m.open_project(_wav(tmp_path, "take", 220), _wav(tmp_path, "guide", 330), project_dir=d))
    return d, [t["id"] for t in r["session"]["tracks"]]


def _tracks(mt):
    return {t["id"]: t for t in _ok(mt.list_tracks())["tracks"]}


def _session_json(d):
    with open(os.path.join(d, "session.json"), encoding="utf-8") as f:
        return json.load(f)


def test_defaults_and_clamping(tmp_path, mcp):
    m, mt = mcp
    d, (t1, t2) = _open(m, tmp_path)
    tr = _tracks(mt)
    assert tr[t1]["gain_db"] == 0.0 and tr[t1]["pan"] == 0.0
    _ok(mt.set_track(t2, gain_db=-3.5, pan=0.3))
    tr = _tracks(mt)
    assert tr[t2]["gain_db"] == -3.5 and tr[t2]["pan"] == 0.3
    assert tr[t1]["gain_db"] == 0.0                      # 渡したトラックだけ
    _ok(mt.set_track(t2, gain_db=40, pan=-5))            # 範囲の外は端に丸める
    tr = _tracks(mt)
    assert tr[t2]["gain_db"] == 6.0 and tr[t2]["pan"] == -1.0
    _ok(mt.set_track(t2, gain_db=-200))                  # −60 以下は無音（−∞）
    assert _tracks(mt)[t2]["gain_db"] == -60.0
    _ok(mt.set_track(t2, gain_db=0, pan=0))
    tr = _tracks(mt)
    assert tr[t2]["gain_db"] == 0.0 and tr[t2]["pan"] == 0.0
    r = mt.set_track(t2, gain_db=float("nan"))
    assert r.get("ok") is False and "gain_db" in r["error"]
    r = mt.set_track(t2, pan=float("inf"))
    assert r.get("ok") is False and "pan" in r["error"]
    # 返り値の session にも入っている
    r = _ok(mt.set_track(t1, gain_db=2.0))
    assert {t["id"]: t["gain_db"] for t in r["session"]["tracks"]}[t1] == 2.0


def test_not_undoable_and_kept_by_undo(tmp_path, mcp):
    m, mt = mcp
    d, (t1, t2) = _open(m, tmp_path)
    _ok(mt.set_track(t2, name="guide vox", author="human"))
    _ok(mt.set_track(t2, gain_db=-6, pan=0.5, author="human"))
    _ok(mt.set_track(t2, offset_sec=0.2, author="human"))
    labels = [e["label"] for e in _session_json(d)["history"]]
    assert labels == ["トラックの名前", "トラックの位置"]        # 音量・パンは履歴に入らない
    # 位置・名前を戻しても音量・パンは今のまま
    _ok(m.undo())
    _ok(m.undo())
    t = _tracks(mt)[t2]
    assert t["offset_sec"] == 0.0 and t["name"] == "guide"
    assert t["gain_db"] == -6.0 and t["pan"] == 0.5
    # 戻した後に別の値にして、やり直しても今の値
    _ok(mt.set_track(t2, gain_db=1.5, pan=-0.25))
    _ok(m.redo())
    _ok(m.redo())
    t = _tracks(mt)[t2]
    assert t["offset_sec"] == 0.2 and t["name"] == "guide vox"
    assert t["gain_db"] == 1.5 and t["pan"] == -0.25
    # 取り消しの対象かどうかの比べ方（structure）にも入らない
    from vocal_engine.project.session import Session
    snap = m._state["session"].snapshot()
    for k in ("gain_db", "pan", "mute", "solo"):
        assert all(k not in x for x in Session.structure(snap)["tracks"])


def test_saved_in_session_and_old_session_loads_defaults(tmp_path, mcp):
    from vocal_engine.project.session import Session
    m, mt = mcp
    d, (t1, t2) = _open(m, tmp_path)
    _ok(mt.set_track(t1, gain_db=-12.25, pan=-0.75))
    saved = {t["id"]: t for t in _session_json(d)["tracks"]}
    assert saved[t1]["gain_db"] == -12.25 and saved[t1]["pan"] == -0.75
    assert saved[t2]["gain_db"] == 0.0 and saved[t2]["pan"] == 0.0
    back = {t["id"]: t for t in Session.load(d).tracks}
    assert back[t1]["gain_db"] == -12.25 and back[t1]["pan"] == -0.75
    # 値の無い古い session.json（このツールができる前）は既定値で読む
    sp = os.path.join(d, "session.json")
    j = _session_json(d)
    for t in j["tracks"]:
        t.pop("gain_db", None)
        t.pop("pan", None)
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(j, f)
    back = {t["id"]: t for t in Session.load(d).tracks}
    assert back[t1]["gain_db"] == 0.0 and back[t1]["pan"] == 0.0
    # 壊れた値も既定値に（読めない文字列・範囲の外）
    for t in j["tracks"]:
        t["gain_db"], t["pan"] = "loud", 9
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(j, f)
    back = {t["id"]: t for t in Session.load(d).tracks}
    assert back[t1]["gain_db"] == 0.0 and back[t1]["pan"] == 1.0


def test_render_tracks_ignores_gain_and_pan(tmp_path, mcp):
    m, mt = mcp
    d, (t1, t2) = _open(m, tmp_path)
    before = {s["id"]: s for s in _ok(mt.render_tracks())["tracks"]}
    _ok(mt.set_track(t1, gain_db=-20, pan=1.0))
    _ok(mt.set_track(t2, gain_db=-60, pan=-1.0))
    after = {s["id"]: s for s in _ok(mt.render_tracks())["tracks"]}
    for tid in (t1, t2):
        assert after[tid]["path"] == before[tid]["path"]
    x, _ = sf.read(after[t2]["path"])
    assert float(np.max(np.abs(x))) > 0.25                  # 元の音のまま（−60 dB を掛けていない）


def test_gliss_file_keeps_gain_and_pan_and_marks_dirty(tmp_path, monkeypatch):
    """.gliss に保存され、開き直しても残る。変えると未保存になる（M／S と同じ）。"""
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_tracks as mt
    from vocal_engine import mcp_document as md
    try:
        _ok(md.new_project())
        t1 = _ok(mt.add_track(_wav(tmp_path, "take", 220), select=True))["track"]
        t2 = _ok(mt.add_track(_wav(tmp_path, "guide", 330)))["track"]
        path = str(tmp_path / "song.gliss")
        _ok(md.save_project(path))
        assert _ok(md.project_status())["document"]["dirty"] is False
        _ok(mt.set_track(t2, gain_db=-4.5, pan=-0.4))
        assert _ok(md.project_status())["document"]["dirty"] is True        # 音量・パンも未保存になる
        _ok(md.save_project())
        assert _ok(md.project_status())["document"]["dirty"] is False
        with open(path, encoding="utf-8") as f:
            saved = {t["id"]: t for t in json.load(f)["session"]["tracks"]}
        assert saved[t2]["gain_db"] == -4.5 and saved[t2]["pan"] == -0.4
        assert saved[t1]["gain_db"] == 0.0 and saved[t1]["pan"] == 0.0
        # 閉じて開き直す
        _ok(md.close_project(discard=True))
        r = _ok(md.load_project(path))
        tr = {t["id"]: t for t in r["session"]["tracks"]}
        assert tr[t2]["gain_db"] == -4.5 and tr[t2]["pan"] == -0.4
        assert tr[t1]["gain_db"] == 0.0 and tr[t1]["pan"] == 0.0
    finally:
        m._state.update(project=None, session=None, track=None, document=None)
        m._invalidate_renderer()
