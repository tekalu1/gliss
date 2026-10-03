# -*- coding: utf-8 -*-
"""無音を戻す（unmute_notes）。合成音だけで確かめる（素材は要らない）。

- 無音の編集だけが外れる（ピッチの編集は残る）・範囲の外の無音は残す・取り消しで無音に戻る
- 無音でないノートは飛ばす（履歴も増えない）・ノートの外へはみ出した無音は残す
"""
import json

import numpy as np
import pytest
import soundfile as sf

from vocal_engine.analysis import f0 as F
from vocal_engine.project.model import Target

SR = 44100
NOTES = [(0.30, 0.90, 220.0), (1.10, 1.70, 247.0), (1.90, 2.50, 277.0)]   # 3 つの長い音（間は無音）


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


def _wav(path):
    x = np.zeros(int(SR * 2.8))
    for a, b, hz in NOTES:
        t = np.arange(int((b - a) * SR)) / SR
        ph = 2 * np.pi * hz * t
        env = np.minimum(1.0, np.minimum(t, (b - a) - t) / 0.03)
        x[int(a * SR):int(a * SR) + len(t)] = env * sum(0.3 / k * np.sin(k * ph) for k in range(1, 8))
    sf.write(path, x, SR, subtype="FLOAT")


def _open(m, tmp_path):
    wav = str(tmp_path / "take.wav")
    _wav(wav)
    _ok(m.open_project(wav, project_dir=str(tmp_path / "p")))
    _ok(m.analyze_take())
    p = m._state["project"]
    ns = [n for n in p.take_notes if n.kind == "note"]
    assert len(ns) >= 3, [(n.id, n.start_sec, n.end_sec) for n in ns]
    return p, ns


def _view(p):
    from vocal_engine.view.export_data import export_view_data
    with open(export_view_data(p)["path"], encoding="utf-8") as f:
        return json.load(f)


def _muted(p):
    return {x["id"]: x["muted"] for x in _view(p)["notes"]}


def test_unmute_restores_sound_and_keeps_pitch_edit(tmp_path, mcp):
    m = mcp
    p, ns = _open(m, tmp_path)
    n = ns[1]
    _ok(m.shift_pitch(cents=80, note_id=n.id, author="human"))
    _ok(m.mute_notes(note_ids=[n.id], author="human"))
    assert _muted(p)[n.id] is True
    r = _ok(m.unmute_notes(note_ids=[n.id], author="human"))
    assert r["note_ids"] == [n.id] and r["history"]["undo"]["label"] == "無音を戻す"
    assert _muted(p)[n.id] is False
    assert not any(e.kind == "mute" for e in p.edits)
    assert any(e.kind == "pitch_shift" for e in p.edits)           # ピッチの編集は残る
    # 取り消すと、また無音
    u = _ok(m.undo())
    assert u["undone"]["label"] == "無音を戻す"
    assert _muted(p)[n.id] is True


def test_unmute_only_the_given_notes_and_skips_the_unmuted(tmp_path, mcp):
    m = mcp
    p, ns = _open(m, tmp_path)
    a, b, c = ns[0], ns[1], ns[2]
    _ok(m.mute_notes(note_ids=[a.id, b.id], author="human"))
    before = len(p.edits)
    # 無音でないノートだけ: 何もしない（履歴も増えない）
    none = _ok(m.unmute_notes(note_ids=[c.id], author="human"))
    assert none["changeset"] is None and len(p.edits) == before
    r = _ok(m.unmute_notes(note_ids=[a.id, c.id], author="human"))
    assert r["note_ids"] == [a.id]                                 # c は無音ではない
    mu = _muted(p)
    assert mu[a.id] is False and mu[b.id] is True and mu[c.id] is False   # 範囲の外の無音は残す


def test_unmute_range_splits_a_mute_that_spans_several_notes(tmp_path, mcp):
    m = mcp
    p, ns = _open(m, tmp_path)
    a, b, c = ns[0], ns[1], ns[2]
    # 3 つをまたぐ 1 つの無音（範囲のミュートを手で足した形）
    p.apply_edits([{"kind": "mute", "target": Target.range(a.start_sec, c.end_sec), "params": {}}],
                  author="human", label="無音")
    assert all(_muted(p)[n.id] for n in (a, b, c))
    _ok(m.unmute_notes(note_ids=[b.id], author="human"))
    mu = _muted(p)
    assert mu[b.id] is False and mu[a.id] is True and mu[c.id] is True
    spans = sorted((e.target.start_sec, e.target.end_sec) for e in p.edits if e.kind == "mute")
    assert len(spans) == 2 and spans[0][1] <= b.start_sec + 1e-6 and spans[1][0] >= b.end_sec - 1e-6


def test_unmute_by_range_and_errors(tmp_path, mcp):
    m = mcp
    p, ns = _open(m, tmp_path)
    _ok(m.mute_notes(note_ids=[n.id for n in ns], author="human"))
    r = _ok(m.unmute_notes(start_sec=ns[0].start_sec + 0.05, end_sec=ns[1].end_sec - 0.05, author="human"))
    assert set(r["note_ids"]) >= {ns[0].id, ns[1].id}
    assert _muted(p)[ns[2].id] is True
    bad = m.unmute_notes(note_ids=["n999"], author="human")
    assert bad.get("ok") is False
