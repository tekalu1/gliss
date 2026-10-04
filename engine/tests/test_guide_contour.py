# -*- coding: utf-8 -*-
"""「ガイドに合わせる」の区間カーブ（`correct_to_guide(pitch_mode="contour")`）。

ノートの対応に頼らず、ガイドの平らな区間ごとにテイクの今の音程との差を打ち消すずらし量カーブを当てる。
"""
import shutil

import numpy as np
import pytest

from conftest import GUIDE, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


@pytest.fixture
def plain(tmp_path):
    from vocal_engine import mcp_server as S
    from vocal_engine.project import Project
    p = Project.open(TAKE, GUIDE, project_dir=str(tmp_path / "plain"))
    p.analyze()
    S._state["project"] = p
    S._state["region"] = {}
    yield p
    S._state["region"] = {}
    shutil.rmtree(p.dir, ignore_errors=True)


def _seg_res(p):
    from vocal_engine.project import guide_contour as GC
    src, out = p.time_map()
    segs = GC.guide_segments(p)
    r = GC.residuals(segs, (np.asarray(src), np.asarray(out)), *GC.edited_midi(p))
    return [abs(x) for x in r if x is not None and abs(x) <= GC.DEFAULT_MAX_SHIFT]


def test_contour_brings_guide_segments_close(plain):
    from vocal_engine import mcp_server as S
    before = _seg_res(plain)
    assert len(before) >= 5 and np.median(before) > 15.0
    r = S.correct_to_guide(pitch_strength=1.0, timing_strength=0.0, pitch_mode="contour")
    assert r["ok"] and r["changeset"], r
    assert r["contour"]["moved_segments"] > 0 and r["contour"]["phrases"] > 0
    assert all(e.kind == "pitch_curve" for e in plain.edits)
    after = _seg_res(plain)
    assert np.median(after) < 6.0, (np.median(before), np.median(after))
    assert len(plain.changesets) == 1
    S.undo()
    assert not plain.edits


def test_contour_with_timing_is_one_changeset_and_keeps_old_curve(plain):
    """前からある pitch_curve（後勝ちで消える）を足し込み、タイミングと一緒に 1 つの changeset で当てる。"""
    from vocal_engine import mcp_server as S
    from vocal_engine.project import timing as TM
    ns = TM.pitched_notes(plain)
    n = max(ns, key=lambda x: x.end_sec - x.start_sec)
    S.set_pitch_curve(points=[[0.0, 40.0], [n.end_sec - n.start_sec, 40.0]], note_id=n.id)
    r = S.correct_to_guide(pitch_strength=1.0, timing_strength=1.0, pitch_mode="contour")
    assert r["ok"] and r["changeset"], r
    kinds = {e.kind for e in plain.edits}
    assert "pitch_curve" in kinds and kinds & {"stretch", "crop", "silence"}
    assert len([c for c in plain.changesets if not c.undone]) == 2
    after = _seg_res(plain)
    assert np.median(after) < 6.0


def test_contour_half_strength_and_max_shift(plain):
    from vocal_engine import mcp_server as S
    before = _seg_res(plain)
    r = S.correct_to_guide(pitch_strength=0.5, timing_strength=0.0, pitch_mode="contour")
    assert r["ok"]
    half = _seg_res(plain)
    assert 0.3 * np.median(before) < np.median(half) < 0.75 * np.median(before)
    S.undo()
    r = S.correct_to_guide(pitch_strength=1.0, timing_strength=0.0, pitch_mode="contour",
                           max_shift_cents=1.0)
    assert r["ok"]
    assert r["contour"]["moved_segments"] == 0 and r["contour"]["skipped_segments"]


def test_measure_reports_guide_segments(plain):
    from vocal_engine import mcp_measure as MM
    from vocal_engine import mcp_server as S
    S.correct_to_guide(pitch_strength=1.0, timing_strength=0.0, pitch_mode="contour")
    m = MM.measure_against_guide()
    assert m["ok"], m
    g = m["summary"]["guide_segments"]
    assert g["n"] > 0
    assert g["edited"]["abs_median"] < 0.5 * g["before"]["abs_median"]
    assert g["after"]["abs_median"] < 0.5 * g["before"]["abs_median"]
    assert g["after"]["within_25"] >= g["before"]["within_25"]
