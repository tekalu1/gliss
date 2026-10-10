# -*- coding: utf-8 -*-
"""複数のノートを 1 回で動かす shift_pitch(notes=…)。合成音だけで確かめる（素材は要らない）。

- 1 回の呼び出し = 1 つの changeset = 取り消し 1 回（ノートごとに違うセントを受ける）
- 要素の cents を省くと引数の cents。note_id・範囲と一緒には渡せない・空や欠けた要素は弾く
- 1 つのノートの呼び出し（今までの形）はそのまま
"""
import numpy as np
import pytest
import soundfile as sf

from vocal_engine.analysis import f0 as F

SR = 44100
NOTES = [(0.30, 0.90, 220.0), (1.10, 1.70, 247.0), (1.90, 2.50, 277.0)]


@pytest.fixture(autouse=True)
def _praat(monkeypatch):
    monkeypatch.setattr(F, "_preferred", None)
    monkeypatch.setenv(F.ESTIMATOR_ENV, "praat")


@pytest.fixture
def mcp():
    from vocal_engine import mcp_server as m
    yield m
    m._state.update(project=None, session=None, track=None)
    m._invalidate_renderer()


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _open(m, tmp_path):
    wav = str(tmp_path / "take.wav")
    x = np.zeros(int(SR * 2.8))
    for a, b, hz in NOTES:
        t = np.arange(int((b - a) * SR)) / SR
        ph = 2 * np.pi * hz * t
        env = np.minimum(1.0, np.minimum(t, (b - a) - t) / 0.03)
        x[int(a * SR):int(a * SR) + len(t)] = env * sum(0.3 / k * np.sin(k * ph) for k in range(1, 8))
    sf.write(wav, x, SR, subtype="FLOAT")
    _ok(m.open_project(wav, project_dir=str(tmp_path / "p")))
    _ok(m.analyze_take())
    p = m._state["project"]
    ns = [n for n in p.take_notes if n.kind == "note"]
    assert len(ns) >= 3
    return p, ns


def test_many_notes_one_changeset_one_undo(tmp_path, mcp):
    m = mcp
    p, ns = _open(m, tmp_path)
    r = _ok(m.shift_pitch(notes=[{"note_id": ns[0].id, "cents": 30},
                                 {"note_id": ns[1].id, "cents": -12.5},
                                 {"note_id": ns[2].id}], cents=100,
                          author="human", label="半音に合わせる"))
    assert len(r["edits"]) == 3
    assert {e["changeset"] for e in r["edits"]} == {r["changeset"]}
    got = {e.target.note_id: e.params["cents"] for e in p.edits}
    assert got == {ns[0].id: 30.0, ns[1].id: -12.5, ns[2].id: 100.0}
    assert r["history"]["undo"]["label"] == "半音に合わせる"
    _ok(m.undo())                                            # 1 回で 3 つとも戻る
    assert not p.edits
    _ok(m.redo())
    assert len(p.edits) == 3


def test_single_note_call_is_unchanged(tmp_path, mcp):
    m = mcp
    p, ns = _open(m, tmp_path)
    r = _ok(m.shift_pitch(40, note_id=ns[1].id, author="human"))
    assert len(r["edits"]) == 1 and r["history"]["undo"]["label"] == "ピッチ"
    assert p.edits[0].params["cents"] == 40.0


@pytest.mark.parametrize("kw", [
    dict(notes=[]),
    dict(notes=[{"cents": 10}]),
    dict(notes=[{"note_id": "__missing__", "cents": 10}]),
    dict(notes=[{"note_id": None}]),
    dict(),
])
def test_rejects_bad_notes(tmp_path, mcp, kw):
    m = mcp
    p, ns = _open(m, tmp_path)
    assert m.shift_pitch(**kw)["ok"] is False
    assert not p.edits


def test_rejects_notes_with_note_id(tmp_path, mcp):
    m = mcp
    p, ns = _open(m, tmp_path)
    r = m.shift_pitch(cents=10, note_id=ns[0].id, notes=[{"note_id": ns[1].id}])
    assert r["ok"] is False and not p.edits
