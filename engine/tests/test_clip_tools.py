# -*- coding: utf-8 -*-
"""トラックビューのはさみ・ミュート（split_track / join_track / mute_track_range）。合成音だけで確かめる（素材は要らない）。

- 切れ目と消した部分（`cuts` / `mutes`）がトラックに入り、list_tracks で返り、保存して開き直しても残る
- 取り消し・やり直し（名前つき）。続けたなぞりは group で 1 回にまとまる。M／S は履歴に入らないまま
- 位置（offset_sec）を動かしても切れ目・消した部分は動かない（トラックの頭が 0 の秒で持つので、一緒に動く）
- 書き出し（export_wav）: 消した区間は 0・前後 5 ms をフェード・それより外は元のサンプルのまま
"""
import json
import os

import numpy as np
import pytest
import soundfile as sf

from vocal_engine.analysis import f0 as F

SR = 44100
NOTES = [(0.30, 0.90, 220.0), (1.10, 1.70, 247.0), (1.90, 2.50, 277.0)]
N_FRAMES = int(SR * 2.8)
DUR = round(N_FRAMES / SR, 6)


@pytest.fixture(autouse=True)
def _praat(monkeypatch):
    monkeypatch.setattr(F, "_preferred", None)
    monkeypatch.setenv(F.ESTIMATOR_ENV, "praat")


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


def _wav(path, subtype="FLOAT"):
    x = np.zeros(N_FRAMES)
    for a, b, hz in NOTES:
        t = np.arange(int((b - a) * SR)) / SR
        ph = 2 * np.pi * hz * t
        env = np.minimum(1.0, np.minimum(t, (b - a) - t) / 0.03)
        x[int(a * SR):int(a * SR) + len(t)] = env * sum(0.3 / k * np.sin(k * ph) for k in range(1, 8))
    sf.write(path, x, SR, subtype=subtype)
    return x


def _open(m, tmp_path, subtype="FLOAT", name="take.wav"):
    wav = str(tmp_path / name)
    _wav(wav, subtype)
    r = _ok(m.open_project(wav, project_dir=str(tmp_path / "p")))
    return wav, r["session"]["tracks"][0]["id"]


def _track(mt, tid):
    return next(t for t in _ok(mt.list_tracks())["tracks"] if t["id"] == tid)


def _labels(mt):
    s = mt._srv._state["session"]
    return [e["label"] for e in s.history if not e.get("undone")]


def test_new_tracks_have_empty_cuts_and_mutes(tmp_path, mcp):
    m, mt = mcp
    _, tid = _open(m, tmp_path)
    t = _track(mt, tid)
    assert t["cuts"] == [] and t["mutes"] == []


def test_split_join_and_mute_range(tmp_path, mcp):
    m, mt = mcp
    _, tid = _open(m, tmp_path)
    r = _ok(mt.split_track(tid, 1.0))
    assert r["track"]["cuts"] == [1.0]
    _ok(mt.split_track(tid, 0.5))
    assert _track(mt, tid)["cuts"] == [0.5, 1.0]
    # 端に近すぎる・もう切れている
    assert mt.split_track(tid, 0.01)["ok"] is False
    assert mt.split_track(tid, DUR - 0.01)["ok"] is False
    assert mt.split_track(tid, 1.0005)["ok"] is False
    # 部分（0.5〜1.0）を消す・隣の部分も消すと 1 つの区間にまとまる
    _ok(mt.mute_track_range(tid, 0.5, 1.0))
    assert _track(mt, tid)["mutes"] == [[0.5, 1.0]]
    _ok(mt.mute_track_range(tid, 1.0, DUR))
    assert _track(mt, tid)["mutes"] == [[0.5, DUR]]
    # 戻す: 真ん中だけ戻すと 2 つに分かれる
    _ok(mt.mute_track_range(tid, 1.2, 1.4, mute=False))
    assert _track(mt, tid)["mutes"] == [[0.5, 1.2], [1.4, DUR]]
    assert mt.mute_track_range(tid, 1.0, 1.0)["ok"] is False
    assert mt.mute_track_range(tid, "x", 1.0)["ok"] is False
    # 範囲の外は丸める
    _ok(mt.mute_track_range(tid, -5, 0.2))
    assert _track(mt, tid)["mutes"][0] == [0.0, 0.2]


def test_join_restores_unless_both_sides_muted(tmp_path, mcp):
    m, mt = mcp
    _, tid = _open(m, tmp_path)
    _ok(mt.split_track(tid, 1.0))
    _ok(mt.mute_track_range(tid, 0.0, 1.0))
    # 片側だけ消えている: つなぐと戻る
    r = _ok(mt.join_track(tid, 1.02))
    assert r["track"]["cuts"] == [] and r["track"]["mutes"] == []
    # 両側が消えている: つないでも消えたまま
    _ok(mt.split_track(tid, 1.0))
    _ok(mt.mute_track_range(tid, 0.0, 1.0))
    _ok(mt.mute_track_range(tid, 1.0, DUR))
    r = _ok(mt.join_track(tid, 1.0))
    assert r["track"]["cuts"] == [] and r["track"]["mutes"] == [[0.0, DUR]]
    # 近くに切れ目が無い
    assert mt.join_track(tid, 2.0)["ok"] is False


def test_clip_edits_are_undoable_with_names_and_mute_solo_are_not(tmp_path, mcp):
    m, mt = mcp
    _, tid = _open(m, tmp_path)
    _ok(mt.split_track(tid, 1.0, author="human"))
    _ok(mt.mute_track_range(tid, 1.0, DUR, author="human"))
    _ok(mt.set_track(tid, mute=True, solo=True))              # 履歴に入らない
    _ok(mt.mute_track_range(tid, 1.2, 1.4, mute=False, author="human"))
    _ok(mt.join_track(tid, 1.0, author="human"))
    assert _labels(mt)[-4:] == ["クリップを分ける", "部分のミュート", "部分を戻す", "クリップをつなぐ"]
    u = _ok(m.undo())
    assert u["undone"]["label"] == "クリップをつなぐ"
    assert _track(mt, tid)["cuts"] == [1.0]
    u = _ok(m.undo())
    assert u["undone"]["label"] == "部分を戻す"
    assert _track(mt, tid)["mutes"] == [[1.0, DUR]]
    t = _track(mt, tid)
    assert t["mute"] is True and t["solo"] is True               # ミュート／ソロは今のまま
    _ok(m.undo())
    assert _track(mt, tid)["mutes"] == []
    _ok(m.undo())
    assert _track(mt, tid)["cuts"] == []
    _ok(m.redo())
    _ok(m.redo())
    t = _track(mt, tid)
    assert t["cuts"] == [1.0] and t["mutes"] == [[1.0, DUR]]


def test_grouped_painting_is_one_undo(tmp_path, mcp):
    m, mt = mcp
    _, tid = _open(m, tmp_path)
    _ok(mt.split_track(tid, 0.9))
    _ok(mt.split_track(tid, 1.8))
    for a, b in ((0.0, 0.9), (0.9, 1.8), (1.8, DUR)):
        _ok(mt.mute_track_range(tid, a, b, group="paint-1", author="human"))
    assert _track(mt, tid)["mutes"] == [[0.0, DUR]]
    u = _ok(m.undo())
    assert u["undone"]["label"] == "部分のミュート"
    assert _track(mt, tid)["mutes"] == [] and _track(mt, tid)["cuts"] == [0.9, 1.8]


def test_position_move_keeps_cuts_and_mutes_with_the_clip(tmp_path, mcp):
    m, mt = mcp
    _, tid = _open(m, tmp_path)
    _ok(mt.split_track(tid, 1.0))
    _ok(mt.mute_track_range(tid, 1.0, 2.0))
    _ok(mt.set_track(tid, offset_sec=0.75, author="human"))
    t = _track(mt, tid)
    # トラックの頭が 0 の秒で持つので、位置を動かしても値は同じ（タイムライン上では 0.75 秒ぶん一緒に動く）
    assert t["offset_sec"] == 0.75 and t["cuts"] == [1.0] and t["mutes"] == [[1.0, 2.0]]


def test_saved_and_reopened(tmp_path, mcp):
    from vocal_engine.project.session import Session
    m, mt = mcp
    _, tid = _open(m, tmp_path)
    _ok(mt.split_track(tid, 1.0))
    _ok(mt.mute_track_range(tid, 1.0, 1.5))
    s = mt._srv._state["session"]
    s2 = Session.load(s.dir)
    t = s2.track(tid)
    assert t["cuts"] == [1.0] and t["mutes"] == [[1.0, 1.5]]
    # session.json の版は 1 のまま（足しただけ）
    with open(s.path, encoding="utf-8") as f:
        assert json.load(f)["version"] == 1


def test_old_session_without_cuts_loads(tmp_path, mcp):
    from vocal_engine.project.session import Session
    m, mt = mcp
    _, tid = _open(m, tmp_path)
    s = mt._srv._state["session"]
    with open(s.path, encoding="utf-8") as f:
        d = json.load(f)
    for t in d["tracks"]:
        t.pop("cuts", None)
        t.pop("mutes", None)
    with open(s.path, "w", encoding="utf-8") as f:
        json.dump(d, f)
    t = Session.load(s.dir).track(tid)
    assert t["cuts"] == [] and t["mutes"] == []


def test_normalizers():
    from vocal_engine.project.session import norm_cuts, norm_mutes, subtract_range
    assert norm_cuts([2.0, 0, -1, 1.0, 1.0004, 9.9, "x", None], 3.0) == [1.0, 2.0]
    assert norm_mutes([[1, 2], [2.0004, 3], [5, 4], [0.5, 0.5], [-1, 0.5]], 3.0) == [[0.0, 0.5], [1.0, 3.0]]
    assert subtract_range([[0.0, 3.0]], 1.0, 2.0) == [[0.0, 1.0], [2.0, 3.0]]
    assert subtract_range([[0.0, 1.0]], 1.0, 2.0) == [[0.0, 1.0]]


# ---------------------------------------------------------------- 書き出し
@pytest.mark.parametrize("subtype", ["FLOAT", "PCM_16"])
def test_export_zeroes_muted_range_with_short_fades(tmp_path, mcp, subtype):
    m, mt = mcp
    wav, tid = _open(m, tmp_path, subtype=subtype)
    _ok(m.analyze_take())
    base = _ok(m.export_wav(path=str(tmp_path / "a.wav"), background=False))
    assert base["muted_spans_sec"] == []
    # 2 つ目の音（1.10〜1.70）を消す
    _ok(mt.split_track(tid, 1.0))
    _ok(mt.split_track(tid, 1.8))
    _ok(mt.mute_track_range(tid, 1.0, 1.8))
    e = _ok(m.export_wav(path=str(tmp_path / "b.wav"), background=False))
    assert e["muted_spans_sec"] == [[1.0, 1.8]]
    assert any("消した区間" in w for w in e["warnings"])
    a, _ = sf.read(str(tmp_path / "a.wav"), dtype="float64", always_2d=True)
    b, sr = sf.read(str(tmp_path / "b.wav"), dtype="float64", always_2d=True)
    assert a.shape == b.shape
    i0, i1 = int(round(1.0 * sr)), int(round(1.8 * sr))
    fade = int(round(0.005 * sr))
    assert np.all(b[i0:i1] == 0)
    assert np.abs(a[i0:i1]).max() > 0.05                         # 元は鳴っていた
    # フェードの外は元のサンプルのまま
    assert np.array_equal(a[:i0 - fade], b[:i0 - fade])
    assert np.array_equal(a[i1 + fade:], b[i1 + fade:])
    # 手前は 1 → 0、後ろは 0 → 1 のなだらかな勾配（元のサンプルより小さい）
    pre, post = slice(i0 - fade, i0), slice(i1, i1 + fade)
    assert np.all(np.abs(b[pre]) <= np.abs(a[pre]) + 1e-9)
    assert np.all(np.abs(b[post]) <= np.abs(a[post]) + 1e-9)
    # 戻すと元に一致する
    _ok(mt.mute_track_range(tid, 1.0, 1.8, mute=False))
    e = _ok(m.export_wav(path=str(tmp_path / "c.wav"), background=False))
    assert e["muted_spans_sec"] == []
    c, _ = sf.read(str(tmp_path / "c.wav"), dtype="float64", always_2d=True)
    assert np.array_equal(a, c)


def test_apply_mutes_edges():
    from vocal_engine.render.export import apply_mutes
    out = np.ones((1000, 2))
    spans = apply_mutes(out, 1000, [[0.0, 0.1], [0.5, 2.0]], "float64")   # 先頭・末尾（フェードは範囲に収める）
    assert spans == [[0.0, 0.1], [0.5, 1.0]]
    assert np.all(out[:100] == 0) and np.all(out[500:] == 0)
    assert out[104, 0] == 1.0 and 0.0 < out[497, 0] < 1.0
