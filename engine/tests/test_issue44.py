# -*- coding: utf-8 -*-
"""Issue #44: 音程のない区間の分割、編集、補正表示、取り消し。"""
import json

import numpy as np
import pytest

from conftest import CLIP_A, CLIP_E, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model, pytest.mark.usefixtures("rmvpe_f0")]   # ノートの ID・区切りは RMVPE の解析のもの
SILENT_TAKE = CLIP_E


@pytest.mark.parametrize("kind,take", [
    ("unvoiced", CLIP_A), ("breath", TAKE), ("silence", SILENT_TAKE),
])
def test_split_nonpitched_edit_and_undo(tmp_path, kind, take):
    from vocal_engine import mcp_server as m
    from vocal_engine.project import Project, timing as TM
    from vocal_engine.view.export_data import export_view_data

    p = Project.open(take, None, project_dir=str(tmp_path / kind))
    p.analyze()
    candidates = [n for n in p.take_notes if n.kind == kind and n.duration_sec > 0.06]
    assert candidates, f"{kind} の分割可能な区間が素材に無い"
    n = candidates[0]
    t = round((n.start_sec + n.end_sec) / 2, 4)
    before = p.time_map()
    m._state["project"] = p
    m._invalidate_renderer()
    try:
        cut = m.split_note(sec=t, note_id=n.id, author="human")
        assert cut["ok"], cut
        right = cut["right"]
        assert [p.note(n.id).kind, p.note(right).kind] == [kind, kind]
        assert p.note(n.id).end_sec == pytest.approx(p.note(right).start_sec)
        after = p.time_map()
        assert np.array_equal(before[0], after[0]) and np.array_equal(before[1], after[1])

        # 新しい境目は接続され、片側の端を動かすともう片側も同じ位置になる。
        inner = TM.plan_edge(p, right, "start")
        assert inner.info["connected"] is True
        dx = min(0.005, inner.x_hi / 2) if inner.x_hi > 0.002 else max(-0.005, inner.x_lo / 2)
        assert abs(dx) > 0.001
        cs_inner, _ = TM.apply_plan(p, inner, dx)
        tm = TM.current_map(p)
        assert tm.at(p.note(n.id).end_sec, "left") == pytest.approx(
            tm.at(p.note(right).start_sec, "right"), abs=1e-6)
        p.undo(cs_inner.id)

        # 分割片を横に動かしても長さは保ち、接続された境目が一緒に動く。
        move = TM.plan_move(p, [right])
        dx = min(0.005, move.x_hi / 2) if move.x_hi > 0.002 else max(-0.005, move.x_lo / 2)
        assert abs(dx) > 0.001
        tm0 = TM.current_map(p)
        width = tm0.at(p.note(right).end_sec, "left") - tm0.at(p.note(right).start_sec, "right")
        cs_move, _ = TM.apply_plan(p, move, dx)
        tm = TM.current_map(p)
        assert tm.at(p.note(right).start_sec, "right") == pytest.approx(t + dx, abs=1e-6)
        assert tm.at(p.note(right).end_sec, "left") - tm.at(p.note(right).start_sec, "right") == pytest.approx(width, abs=1e-6)
        p.undo(cs_move.id)

        # 右端の計画の見かけと確定後、補正表示用の値が一致する。
        plan = TM.plan_edge(p, right, "end")
        x = min(0.01, plan.x_hi / 2) if plan.x_hi > 0.002 else max(-0.01, plan.x_lo / 2)
        assert abs(x) > 0.001
        expected = plan.new_positions(x)
        cs, _ = TM.apply_plan(p, plan, x)
        assert cs is not None
        tm = TM.current_map(p)
        ks, ke = plan.st.note_knots[right]
        assert tm.at(p.note(right).end_sec, "left") == pytest.approx(expected[ke], abs=1e-6)
        data = json.loads(open(export_view_data(p)["path"], encoding="utf-8").read())
        by = {v["id"]: v for v in data["notes"]}
        assert by[right]["timing_corr"]["manual"] is True
        assert by[right]["timing_corr"]["degree"] > 0
        assert by[right]["pitch_corr"] is None
        assert by[right]["start_sec"] == pytest.approx(t, abs=1e-4)
        p.undo()
        assert TM.current_map(p).at(p.note(right).end_sec, "left") == pytest.approx(n.end_sec)

        # 自動補正は白ではなく移動量に応じた黄→赤の度合いを返す。
        removes, specs, _ = TM.realize(p, TM.plan_edge(p, right, "end"), x)
        p.apply_changes(removes, specs, author="human", origin="auto")
        auto = {v["id"]: v for v in json.loads(open(export_view_data(p)["path"], encoding="utf-8").read())["notes"]}
        assert auto[right]["timing_corr"]["manual"] is False
        assert 0 < auto[right]["timing_corr"]["degree"] <= 1
        p.undo()

        # 結合と undo: 同じ種類の区間に戻り、分割境界も復元できる。
        merged = m.merge_notes(n.id, right, author="human")
        assert merged["ok"] and merged["removed_split"]
        assert p.note(n.id).kind == kind and p.note(n.id).end_sec == pytest.approx(n.end_sec)
        p.undo()
        assert p.note(right).kind == kind
        p.undo()
        assert [v.id for v in p.take_notes if v.id == right] == []
    finally:
        m._state.update(project=None, session=None, track=None)
        m._invalidate_renderer()
