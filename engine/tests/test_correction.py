# -*- coding: utf-8 -*-
"""補正の度合いと手動／自動（issue #37。`project/correction.py`）。

- 度合い = 元からどれだけ動いたか ÷ 基準（タイミング 80 ms、ピッチ 100 セント）。今の状態から測る
- 最後に動かしたのが「ガイドに合わせる」なら自動、ドラッグ・shift_pitch などなら手動
- 取り消し・やり直しで印も戻る。接続された隣が一緒に動けば、その隣も同じ由来
- 画面用データ（export_view_data）の notes[].timing_corr / pitch_corr と correction_ref
"""
import json

import numpy as np
import pytest

from conftest import needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


def _view(p):
    from vocal_engine.view.export_data import export_view_data
    r = export_view_data(p)
    with open(r["path"], encoding="utf-8") as f:
        d = json.load(f)
    return d, {n["id"]: n for n in d["notes"]}


def _pitched(p):
    return [n for n in p.take_notes if n.kind == "note"]


def test_no_edit_no_correction(project):
    project.analyze()
    d, by = _view(project)
    assert d["correction_ref"] == {"timing_ms": 80.0, "pitch_cents": 100.0}
    assert all(n["timing_corr"] is None and n["pitch_corr"] is None for n in by.values())


def test_manual_pitch_and_timing(project):
    from vocal_engine.project import timing as TM
    from vocal_engine.project.model import Target
    project.analyze()
    n = _pitched(project)[3]
    project.apply_edits([{"kind": "pitch_shift", "target": Target.note(n.id),
                          "params": {"cents": 40.0}}], author="human")
    plan = TM.plan_move(project, [n.id])
    TM.apply_plan(project, plan, 0.02)
    _, by = _view(project)
    pc, tc = by[n.id]["pitch_corr"], by[n.id]["timing_corr"]
    assert pc["manual"] is True and pc["amount_cents"] == pytest.approx(40.0, abs=0.5)
    assert pc["degree"] == pytest.approx(0.4, abs=0.01) and pc["shape"] is False
    assert tc["manual"] is True and tc["amount_ms"] == pytest.approx(20.0, abs=0.5)
    assert tc["degree"] == pytest.approx(20.0 / 80.0, abs=0.01)
    # 動かしていないノートには何も付かない（隣は接続なら長さが変わる = タイミングの補正）
    far = _pitched(project)[-1]
    assert by[far.id]["timing_corr"] is None and by[far.id]["pitch_corr"] is None


def test_guide_is_auto_then_manual_overrides_and_undo_restores(project):
    from vocal_engine.project import timing as TM
    from vocal_engine.project.model import Target
    project.analyze()
    plan = TM.plan_guide(project, match_pitch_shape=False)
    assert plan.pitch, "ガイドに合わせるピッチの対象が無い"
    nid = next(iter(plan.pitch))
    TM.apply_plan(project, plan, 1.0, pitch=0.5)
    _, by = _view(project)
    pc = by[nid]["pitch_corr"]
    assert pc is not None and pc["manual"] is False
    want = abs(plan.pitch[nid] * 0.5)
    assert pc["amount_cents"] == pytest.approx(want, abs=0.6)
    assert pc["degree"] == pytest.approx(min(1.0, want / 100.0), abs=0.01)
    autos_t = [i for i, v in by.items() if v["timing_corr"] and not v["timing_corr"]["manual"]]
    assert autos_t, "タイミングを自動で動かしたノートが無い"
    # 後から手動でピッチを動かすと、そのノートのピッチだけ手動（最後にかけた方）
    project.apply_edits([{"kind": "pitch_shift", "target": Target.note(nid),
                          "params": {"cents": 10.0}}], author="human")
    _, by2 = _view(project)
    assert by2[nid]["pitch_corr"]["manual"] is True
    if by[nid]["timing_corr"]:
        assert by2[nid]["timing_corr"]["manual"] is False
    # 取り消すと自動に戻る。やり直すと手動
    project.undo()
    _, by3 = _view(project)
    assert by3[nid]["pitch_corr"]["manual"] is False
    project.redo()
    _, by4 = _view(project)
    assert by4[nid]["pitch_corr"]["manual"] is True


def test_reset_to_zero_clears(project):
    from vocal_engine.project.model import Target
    project.analyze()
    n = _pitched(project)[2]
    project.apply_edits([{"kind": "pitch_shift", "target": Target.note(n.id),
                          "params": {"cents": 30.0}}], author="human")
    project.apply_edits([{"kind": "pitch_shift", "target": Target.note(n.id),
                          "params": {"cents": -30.0}}], author="human")
    _, by = _view(project)
    assert by[n.id]["pitch_corr"] is None


def test_degree_saturates_and_neighbour_follows(project):
    from vocal_engine.project import timing as TM
    project.analyze()
    ns = _pitched(project)
    # 接続された境目を持つノートの尻を大きく伸ばすと、次のノートの頭も動く（同じ由来 = 手動）
    k = next(i for i in range(len(ns) - 1)
             if abs(ns[i + 1].start_sec - ns[i].end_sec) < 1e-6)
    a, b = ns[k], ns[k + 1]
    plan = TM.plan_edge(project, a.id, "end")
    x = min(plan.x_hi, 0.12)
    TM.apply_plan(project, plan, x)
    _, by = _view(project)
    ta, tb = by[a.id]["timing_corr"], by[b.id]["timing_corr"]
    assert ta["manual"] and tb["manual"]
    assert ta["amount_ms"] == pytest.approx(x * 1000, abs=0.5)
    assert ta["degree"] == pytest.approx(min(1.0, x * 1000 / 80.0), abs=0.01)


def test_marks_live_in_changesets_and_survive_reload(project):
    from vocal_engine.project import Project
    from vocal_engine.project.model import Target
    project.analyze()
    n = _pitched(project)[1]
    cs, _ = project.apply_edits([{"kind": "pitch_shift", "target": Target.note(n.id),
                                  "params": {"cents": 25.0}}], author="human")
    marks = [o for o in cs.ops if o.get("op") == "mark"]
    assert marks == [{"op": "mark", "origin": "manual", "timing": [], "pitch": [n.id]}]
    q = Project(project.dir)
    q.load()
    q.analyze()
    from vocal_engine.project.correction import origins
    _, pit = origins(q)
    assert pit.get(n.id) == "manual"


def test_timing_shift_function():
    from vocal_engine.project.correction import timing_shift
    src = np.array([0.0, 1.0, 2.0, 3.0])
    out = np.array([0.0, 1.05, 2.0, 3.0])
    assert timing_shift(src, out, 0.5, 1.5) == pytest.approx(0.05)
    assert timing_shift(src, out, 2.2, 2.8) == pytest.approx(0.0)
    # 無音の挿入（同じ編集前の秒に 2 値）: 尻は手前、頭は後ろの値
    src2 = np.array([0.0, 1.0, 1.0, 3.0])
    out2 = np.array([0.0, 1.0, 1.2, 3.2])
    assert timing_shift(src2, out2, 0.0, 1.0) == pytest.approx(0.0)
    assert timing_shift(src2, out2, 1.0, 2.0) == pytest.approx(0.2)


def test_pencil_degree_uses_drawn_curve(project):
    from vocal_engine.project.pitch import draw_specs

    project.analyze()
    n = next(n for n in _pitched(project) if n.end_sec - n.start_sec > 0.2)
    a = n.start_sec + 0.06
    b = min(n.end_sec - 0.06, a + 0.12)
    target = n.pitch_midi + 1.5
    remove, specs, _ = draw_specs(project, [[a, target], [b, target]])
    project.apply_changes(remove, specs, author="human")
    _, by = _view(project)
    pc = by[n.id]["pitch_corr"]
    assert pc["manual"] is True and pc["shape"] is True
    assert pc["amount_cents"] > 80
    assert pc["degree"] > 0.8
