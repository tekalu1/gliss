# -*- coding: utf-8 -*-
"""子音・息（音程の無いノート）の幅とタイミング。音程のあるノートと同じ 1 つの規則（`project/timing.py` の冒頭）:
接している隣とは境目を共有、離れていれば隙間が変わる、Alt で自分だけ。素材の要る試験（素材なしの試験は test_single_timing_rule.py）。

- 子音・息の端・移動も、音程のあるノートと同じ計画（plan_edge / plan_move）で動く
- 接していれば接続（隣が伸び縮み）。Alt（detach）で切り離すと隙間ができ、`connection` 編集に残る。吸着で戻る
- アタック・境目の子音の特別な規則や、挟んでいる音程ノートの組の接続の引き継ぎは無い
- 確定した結果 = 計画の式（ドラッグ中の見た目 = 離した後）。編集した所と隣の外は動かない。undo で戻る
- 無音は区間ではなく隙間（動かさない）
"""
import shutil

import pytest

from conftest import CLIP_A, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


def _open(tmp_path, take, name):
    from vocal_engine.project import Project
    p = Project.open(take, None, project_dir=str(tmp_path / name))
    p.analyze(auto_lyrics=False)
    return p


@pytest.fixture
def pa(tmp_path):
    """素材 A: 音程ノートに挟まれた無声（子音）がある。"""
    p = _open(tmp_path, CLIP_A, "a")
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)


@pytest.fixture
def pc(tmp_path):
    """素材 C: 音程ノートに挟まれた息（breath）がある（前後の組は既定で切り離し）。"""
    p = _open(tmp_path, TAKE, "c")
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)


def _between(p, kind):
    """kind のノートで、前後が接した音程ノートのもの (前, それ, 次)。"""
    ns = p.take_notes
    for i in range(1, len(ns) - 1):
        a, u, b = ns[i - 1], ns[i], ns[i + 1]
        if (u.kind == kind and a.kind == "note" and b.kind == "note"
                and abs(u.start_sec - a.end_sec) < 1e-6 and abs(b.start_sec - u.end_sec) < 1e-6):
            return a, u, b
    pytest.skip("%s を挟んだ音程ノートが無い" % kind)


def _edges(p):
    from vocal_engine.project import timing as TM
    tm = TM.current_map(p)
    return {n.id: (tm.at(n.start_sec, "right"), tm.at(n.end_sec, "left")) for n in p.take_notes}


def _check(p, plan, x):
    """確定した後のノートの頭・尻 = 計画の式（cur + d*x）。"""
    pos = plan.new_positions(x)
    e = _edges(p)
    for nid, (ks, ke) in plan.st.note_knots.items():
        assert abs(e[nid][0] - pos[ks]) < 1e-6, nid
        assert abs(e[nid][1] - pos[ke]) < 1e-6, nid


def _unchanged(before, after, skip):
    for nid in before:
        if nid in skip:
            continue
        assert abs(after[nid][0] - before[nid][0]) < 1e-9, "%s の頭が動いた" % nid
        assert abs(after[nid][1] - before[nid][1]) < 1e-9, "%s の尻が動いた" % nid


def test_consonant_edge_and_move_connected(pa):
    """接した子音の尻を伸ばすと次のノートが縮む。移動すると長さを保って前が伸び・次が縮む。undo で戻る。"""
    from vocal_engine.project import timing as TM
    a, u, b = _between(pa, "unvoiced")
    e0 = _edges(pa)
    plan = TM.plan_edge(pa, u.id, "end")
    assert plan.info["neighbour"] == b.id and plan.info["connected"] is True
    assert plan.x_lo < -0.01 and plan.x_hi > 0.01
    cs, _ = TM.apply_plan(pa, plan, 0.03)
    assert cs is not None
    _check(pa, plan, 0.03)
    e1 = _edges(pa)
    assert abs(e1[u.id][0] - e0[u.id][0]) < 1e-9                     # 頭は動かない
    assert abs(e1[u.id][1] - (e0[u.id][1] + 0.03)) < 1e-6            # 尻が 30 ms 後ろへ
    assert abs(e1[b.id][0] - e1[u.id][1]) < 1e-9                      # 次の頭は境目を共有
    assert abs(e1[b.id][1] - e0[b.id][1]) < 1e-9                      # 次の尻は動かない
    _unchanged(e0, e1, {u.id, b.id})
    # 移動: 長さはそのまま、前の尻と次の頭が一緒に動く
    plan = TM.plan_move(pa, [u.id])
    cs2, _ = TM.apply_plan(pa, plan, -0.02)
    _check(pa, plan, -0.02)
    e2 = _edges(pa)
    L1 = e1[u.id][1] - e1[u.id][0]
    assert abs((e2[u.id][1] - e2[u.id][0]) - L1) < 1e-6
    assert abs(e2[a.id][1] - (e1[a.id][1] - 0.02)) < 1e-6
    assert abs(e2[b.id][0] - (e1[b.id][0] - 0.02)) < 1e-6
    _unchanged(e1, e2, {a.id, u.id, b.id})
    src, out = pa.time_map()
    assert abs(out[-1] - src[-1]) < 1e-9, "素材の末尾がずれた（リップル）"
    pa.undo(cs2.id)
    pa.undo(cs.id)
    e3 = _edges(pa)
    _unchanged(e0, e3, set())


def test_consonant_alt_detach_and_snap_back(pa):
    """Alt で子音の頭を切り離すと前のノートは動かず隙間ができる（接続の編集が残る）。ぶつかる所で離すとつながる。"""
    from vocal_engine.project import timing as TM
    a, u, b = _between(pa, "unvoiced")
    e0 = _edges(pa)
    plan = TM.plan_edge(pa, u.id, "start", detach=True)
    assert plan.set_connections == [(a.id, u.id, False)]
    assert plan.x_lo == 0.0                                           # 前には伸ばせない（接している）
    cs, _ = TM.apply_plan(pa, plan, 0.02)
    _check(pa, plan, 0.02)
    e1 = _edges(pa)
    assert abs(e1[a.id][1] - e0[a.id][1]) < 1e-9                      # 前のノートは動かない
    assert abs(e1[u.id][0] - (e0[u.id][0] + 0.02)) < 1e-6
    conn = [e.params for e in pa.edits if e.kind == "connection"]
    assert conn == [{"a": a.id, "b": u.id, "connected": False}]
    # 切り離した後: 頭は自分だけ動き、元の位置（隙間 0）が吸着の位置
    plan = TM.plan_edge(pa, u.id, "start")
    assert plan.info["connected"] is False and plan.snap_pair == (a.id, u.id)
    assert abs(plan.snap_x - (-0.02)) < 1e-6
    TM.apply_plan(pa, plan, plan.snap_x)
    assert not [e for e in pa.edits if e.kind == "connection"]        # 既定（接続）に戻ったので上書きは外す
    e2 = _edges(pa)
    _unchanged(e0, e2, set())


def test_old_override_on_the_pitched_pair_is_not_inherited(pa):
    """挟んでいる音程ノートの組（a, b）の古い切り離しは、子音の接続に引き継がない（接していれば接続。種類によらない）。"""
    from vocal_engine.project import timing as TM
    from vocal_engine.project.model import Target
    a, u, b = _between(pa, "unvoiced")
    pa.apply_changes([], [{"kind": "connection", "target": Target.range(a.end_sec, b.start_sec),
                           "params": {"a": a.id, "b": b.id, "connected": False}}], author="human", label="古い切り離し")
    assert TM.connection_specs(pa, [(a.id, b.id, False)]) == ([], [])      # 隣り合わない組は新しく覚えない
    assert (a.id, b.id) not in TM.connection_map(pa)
    assert TM.plan_edge(pa, u.id, "start").info["connected"] is True
    assert TM.plan_edge(pa, u.id, "end").info["connected"] is True
    st, _ = TM.build_structure(pa)
    assert u.id in st.note_knots and not any(k.role == "attack" for k in st.knots)      # アタックの規則は無い


def test_breath_moves_with_connected_neighbours(pc):
    """息も動かせる（接していれば接続。前の尻と次の頭が一緒に動く）。確定 = 計画の式。"""
    from vocal_engine.project import timing as TM
    a, u, b = _between(pc, "breath")
    cm = TM.connection_map(pc)
    assert cm[(a.id, u.id)] is True and cm[(u.id, b.id)] is True       # 接していれば、種類によらず接続
    assert (a.id, b.id) not in cm                                      # 息を挟んだ音程ノートの組は、隣り合う組ではない
    e0 = _edges(pc)
    plan = TM.plan_move(pc, [u.id])
    assert plan.x_lo < -0.05 and plan.x_hi > 0.05
    TM.apply_plan(pc, plan, 0.05)
    _check(pc, plan, 0.05)
    e1 = _edges(pc)
    assert abs(e1[a.id][1] - (e0[a.id][1] + 0.05)) < 1e-6
    assert abs(e1[b.id][0] - (e0[b.id][0] + 0.05)) < 1e-6
    _unchanged(e0, e1, {a.id, u.id, b.id})


def test_silence_is_not_movable_and_every_block_is_in_the_structure(pa, pc):
    from vocal_engine.project import timing as TM
    sil = next((n for p in (pa, pc) for n in p.take_notes if n.kind == "silence"), None)
    if sil is not None:
        p = pa if sil in pa.take_notes else pc
        with pytest.raises(TM.TimingError):
            TM.plan_edge(p, sil.id, "end")
        with pytest.raises(TM.TimingError):
            TM.plan_move(p, [sil.id])
    # 骨組みは、音程のあるノート・子音・息のすべて（どの区間を操作しても同じ。無音は入らない）
    st, _ = TM.build_structure(pa)
    assert set(st.note_knots) == {n.id for n in TM.blocks(pa)}
    assert not any(n.kind == "silence" and n.id in st.note_knots for n in pa.take_notes)


def test_mcp_move_note_and_stretch_accept_consonant(tmp_path):
    """MCP の move_note / stretch も子音のノートに効く（取り消しの履歴に入る）。"""
    from vocal_engine import mcp_server as m
    try:
        r = m.open_project(CLIP_A, None, project_dir=str(tmp_path / "mcp"))
        assert r.get("ok") is not False, r.get("error")
        assert m.analyze_take().get("ok") is not False
        p = m._state["project"]
        a, u, b = _between(p, "unvoiced")
        r = m.move_note(ms=15, note_id=u.id, author="human")
        assert r.get("ok") is not False, r.get("error")
        r = m.stretch(ratio=0.8, note_id=u.id, author="human")
        assert r.get("ok") is not False, r.get("error")
        assert r["history"]["undo"]["label"] == "ノートの長さ"
        assert m.undo().get("ok") is not False and m.undo().get("ok") is not False
        assert not [e for e in p.edits if e.kind in ("stretch", "crop", "silence")]
    finally:
        m._state.update(project=None, session=None, track=None)
        m._invalidate_renderer()
