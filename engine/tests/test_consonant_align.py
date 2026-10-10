# -*- coding: utf-8 -*-
"""子音・息を、音程のあるノートと揃える（2026-10-10 承認。エンジン側。素材なし・合成のノート列）。

- オリジナルに戻す（reset_to_original）が、子音・息のタイミング（頭・尻）も元に戻す（音程のあるノートと同じ）
"""
import json

import numpy as np
import pytest
import soundfile as sf

from vocal_engine.analysis.notes import Note

SR = 22050

# ノート・子音・ノート・息・ノート。すべて接している
TOUCHING = [(0.10, 0.50, "note"), (0.50, 0.70, "unvoiced"), (0.70, 1.10, "note"),
            (1.10, 1.30, "breath"), (1.30, 1.70, "note")]
# 離れている（隙間 0.10）
APART = [(0.10, 0.50, "note"), (0.60, 0.80, "unvoiced"), (0.90, 1.30, "note"),
         (1.40, 1.60, "breath"), (1.70, 2.10, "note")]


def _note(i, a, b, kind):
    return Note(id="n%02d" % i, start_sec=a, end_sec=b, kind=kind,
                label="sung" if kind == "note" else kind, source="take", text=None,
                pitch_hz=220.0 if kind == "note" else None, pitch_midi=57.0 if kind == "note" else None,
                note_name="A3" if kind == "note" else None, iqr_semitones=0.1 if kind == "note" else None,
                rms_peak_db=-20.0, confidence=1.0, start_frame=int(a * 100), end_frame=int(b * 100))


@pytest.fixture
def mcp(tmp_path):
    from vocal_engine import mcp_server as m
    from vocal_engine.analysis import f0 as F
    from vocal_engine.project import Project
    F.set_preferred_estimator("praat")
    made = {}

    def build(spans):
        t = np.arange(int(2.4 * SR)) / SR
        path = tmp_path / "v.wav"
        rng = np.random.default_rng(1)
        y = 0.2 * np.sin(2 * np.pi * 220 * t) + 0.05 * rng.standard_normal(len(t))
        sf.write(str(path), y.astype("float32"), SR)
        p = Project.open(str(path), None, project_dir=str(tmp_path / "p"))
        p.analyze(auto_lyrics=False)
        p._take_notes = sorted([_note(i, a, b, k) for i, (a, b, k) in enumerate(spans)],
                               key=lambda n: n.start_sec)
        p._notes_cache = None
        m._state.update(project=p, session=None, track=None)
        m._invalidate_renderer()
        made["p"] = p
        return m, p, str(path)

    yield build
    m._state.update(project=None, session=None, track=None)
    m._invalidate_renderer()
    F.set_preferred_estimator(None)


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _edges(p):
    from vocal_engine.project import timing as TM
    tm = TM.current_map(p)
    return {n.id: (tm.at(n.start_sec, "right"), tm.at(n.end_sec, "left")) for n in p.take_notes}


def _view(p):
    from vocal_engine.view.export_data import export_view_data
    with open(export_view_data(p)["path"], encoding="utf-8") as f:
        return {n["id"]: n for n in json.load(f)["notes"]}


def _export(p, path, src):
    from vocal_engine.render.export import export_wav
    export_wav(p, path=str(path))
    a, sr = sf.read(src, dtype="float64", always_2d=True)
    b, _ = sf.read(str(path), dtype="float64", always_2d=True)
    assert a.shape == b.shape
    return a, b, sr


# ================================================================ オリジナルに戻す（タイミング）
@pytest.mark.parametrize("kind_id", ["n01", "n03"])              # 子音 / 息
def test_reset_restores_the_timing_of_a_consonant_or_breath(mcp, kind_id):
    from vocal_engine.project import timing as TM
    m, p, _ = mcp(TOUCHING)
    before = _edges(p)
    plan = TM.plan_move(p, [kind_id])
    TM.apply_plan(p, plan, 0.03)
    moved = _edges(p)
    assert moved[kind_id][0] == pytest.approx(before[kind_id][0] + 0.03, abs=1e-6)   # 動いた（前提）
    r = _ok(m.reset_to_original(note_ids=[kind_id], author="human"))
    assert r["changeset"] is not None
    after = _edges(p)
    for nid, e in before.items():                                 # 子音・息だけでなく、接した隣も元どおり
        assert after[nid] == pytest.approx(e, abs=1e-6), nid
    _ok(m.undo())                                                 # 1 回の取り消しで動かした状態に戻る
    again = _edges(p)
    assert again[kind_id][0] == pytest.approx(moved[kind_id][0], abs=1e-6)


def test_reset_of_a_consonant_leaves_the_pitched_neighbours_pitch_edits(mcp):
    """子音を戻しても、隣の音程ノートのピッチの編集は外さない（外すのは渡した区間のピッチだけ）。"""
    from vocal_engine.project import timing as TM
    m, p, _ = mcp(TOUCHING)
    _ok(m.shift_pitch(cents=100, note_id="n00", author="human"))
    plan = TM.plan_edge(p, "n01", "end")
    TM.apply_plan(p, plan, 0.02)
    _ok(m.reset_to_original(note_ids=["n01"], author="human"))
    assert any(e.kind == "pitch_shift" for e in p.edits)
    assert _edges(p)["n01"][1] == pytest.approx(0.70, abs=1e-6)


def test_reset_by_range_restores_the_timing_of_a_consonant(mcp):
    from vocal_engine.project import timing as TM
    m, p, _ = mcp(TOUCHING)
    before = _edges(p)
    TM.apply_plan(p, TM.plan_move(p, ["n01"]), 0.03)
    _ok(m.reset_to_original(start_sec=0.4, end_sec=0.8, author="human"))
    after = _edges(p)
    for nid, e in before.items():
        assert after[nid] == pytest.approx(e, abs=1e-6), nid


def test_reset_of_an_apart_breath_restores_its_timing_and_keeps_the_gaps(mcp):
    from vocal_engine.project import timing as TM
    m, p, _ = mcp(APART)
    before = _edges(p)
    TM.apply_plan(p, TM.plan_edge(p, "n03", "end"), -0.04)         # 息を縮める（隙間が広がる）
    assert _edges(p)["n03"][1] != pytest.approx(before["n03"][1], abs=1e-6)
    _ok(m.reset_to_original(note_ids=["n03"], author="human"))
    after = _edges(p)
    for nid, e in before.items():
        assert after[nid] == pytest.approx(e, abs=1e-6), nid
