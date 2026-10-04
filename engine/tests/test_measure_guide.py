# -*- coding: utf-8 -*-
"""補正の前後のノートごとの残差（`measure_against_guide`）・`list_deviations` の発音の頭の物差し・
音程の編集をまとめて当てる（`apply_edits`）。

タイミングの物差しは correct_to_guide が合わせる発音の頭の組。前は remeasure / list_deviations が
ノートの頭で測っていたので、タイミングを 100% 合わせても値が下がらず、直ったことが分からなかった。
"""
import shutil

import numpy as np
import pytest

from conftest import GUIDE, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


@pytest.fixture
def plain(tmp_path):
    from vocal_engine import mcp_server as S
    from vocal_engine import mcp_measure as MM
    from vocal_engine.project import Project
    p = Project.open(TAKE, GUIDE, project_dir=str(tmp_path / "plain"))
    p.analyze()
    S._state["project"] = p
    S._state["region"] = {}
    yield p
    S._state["region"] = {}
    shutil.rmtree(p.dir, ignore_errors=True)


def _vals(rows, key, which):
    return [r[key][which] for r in rows if r[key][which] is not None]


def test_measure_without_edits_matches_before(plain):
    from vocal_engine import mcp_server as S
    from vocal_engine import mcp_measure as MM
    r = MM.measure_against_guide()
    assert r["ok"], r
    rows = r["rows"]
    assert rows and r["summary"]["matched_notes"] > 0 and r["summary"]["onset_paired_notes"] > 0
    for row in rows:
        pc = row["pitch_cents"]
        if pc["before"] is not None:
            assert pc["edited"] == pytest.approx(pc["before"], abs=1.0), row["note_id"]
        tm = row["timing_ms"]
        if tm["before"] is not None:
            assert tm["edited"] == tm["before"]
    diff = [abs(x["pitch_cents"]["after"] - x["pitch_cents"]["before"]) for x in rows
            if x["pitch_cents"]["after"] is not None and x["pitch_cents"]["before"] is not None]
    assert np.median(diff) < 5.0, "編集していない音は測り直しても同じ"


def test_measure_shows_correction(plain):
    """ピッチ・タイミング 100% の後は、音程・発音の頭とも edited / after が before より小さい。"""
    from vocal_engine import mcp_server as S
    from vocal_engine import mcp_measure as MM
    r = S.correct_to_guide(pitch_strength=1.0, timing_strength=1.0, match_pitch_shape=False)
    assert r["ok"] and r["changeset"]
    m = MM.measure_against_guide()
    assert m["ok"], m
    s = m["summary"]
    assert s["pitch_cents"]["edited"]["abs_median"] < 0.5 * s["pitch_cents"]["before"]["abs_median"]
    assert s["pitch_cents"]["after"]["abs_median"] < 0.5 * s["pitch_cents"]["before"]["abs_median"]
    assert s["timing_ms"]["edited"]["abs_median"] < s["timing_ms"]["before"]["abs_median"]
    assert s["timing_ms"]["after"]["n"] > 0
    assert s["timing_ms"]["after"]["abs_median"] < s["timing_ms"]["before"]["abs_median"]
    # 編集後の秒が返る（タイミングを動かしたノートは編集前と違う）
    assert any(abs(x["edited_start_sec"] - x["start_sec"]) > 1e-3 for x in m["rows"])


def test_list_deviations_onset_timing_follows_edits(plain):
    from vocal_engine import mcp_server as S
    from vocal_engine import mcp_measure as MM
    before = S.list_deviations(threshold_ms=10.0)["onset_timing"]
    assert before["pairs"] > 0
    assert before["edited_ms"]["abs_median"] == before["before_ms"]["abs_median"]
    S.correct_to_guide(pitch_strength=0.0, timing_strength=1.0)
    after = S.list_deviations(threshold_ms=10.0)["onset_timing"]
    assert after["before_ms"] == before["before_ms"], "before は元の音のまま"
    assert after["edited_ms"]["abs_median"] < before["before_ms"]["abs_median"]
    assert after["over_threshold_edited"] < before["over_threshold_before"]


def test_suggested_edits_apply_in_one_changeset(plain):
    from vocal_engine import mcp_server as S
    from vocal_engine import mcp_measure as MM
    m = MM.measure_against_guide(render=False, suggest_cents=20.0, threshold_cents=20.0)
    assert m["ok"] and m["suggested_edits"]
    assert all(abs(x["pitch_cents"]["edited"]) >= 20.0 for x in m["rows"])
    n_cs = len(plain.changesets)
    r = MM.apply_edits(edits=m["suggested_edits"])
    assert r["ok"] and r["applied"] == len(m["suggested_edits"])
    assert len(plain.changesets) == n_cs + 1
    m2 = MM.measure_against_guide(render=False)
    fixed = {x["note_id"] for x in m["suggested_edits"]}
    for row in m2["rows"]:
        if row["note_id"] in fixed:
            assert abs(row["pitch_cents"]["edited"]) < 1.0, row
    S.undo()
    assert not plain.edits, "取り消し 1 回で全部戻る"


def test_apply_edits_rejects_bad_entry_without_partial(plain):
    from vocal_engine import mcp_server as S
    from vocal_engine import mcp_measure as MM
    from vocal_engine.project import timing as TM
    n = TM.pitched_notes(plain)[0]
    r = MM.apply_edits(edits=[{"op": "shift_pitch", "cents": 10, "note_id": n.id},
                             {"op": "shift_pitch", "cents": 10, "note_id": "nope"}])
    assert not r["ok"] and "edits[1]" in r["error"]
    assert not plain.edits
    r = MM.apply_edits(edits=[{"op": "shift_pitch", "cents": 10, "note_id": n.id},
                             {"op": "set_pitch_curve", "mode": "draw",
                              "points": [[n.start_sec, 60.0], [n.end_sec, 60.5]]},
                             {"op": "shift_pitch", "cents": -5, "start_sec": n.start_sec,
                              "end_sec": n.end_sec}])
    assert r["ok"] and r["applied"] == 3
    assert [e.kind for e in plain.edits] == ["pitch_shift", "pitch_draw", "pitch_shift"]
