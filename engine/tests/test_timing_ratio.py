# -*- coding: utf-8 -*-
"""「ガイドに合わせる」のタイミングの割合をノートごとに変える（`project/timing_ratio.py`。2026-10-11 承認・案 T1）。

素材は使わない（合成の歌声。test_strength.py と同じ曲で、ガイドの頭がノートごとにずれている）。解析モデルの重みだけ要る。

- 全部のノートを同じ割合 s にすると、今までの `apply_plan(x=s)` と同じ位置（s = 0.3・0.7・1.0）
- 1 つのノートだけ 100% → 30% にすると、そのノートと、接している前のノートの尻だけが動く
- 0.7 → 0.3 → 0.7 で同じ位置に戻る。取り消し 1 回で戻る。ポップアップの当て直しは履歴 1 つ
- 手で直したノートは既定で変えない（含めると手のずれを残す）
- 隙間を挟んだ前のノートの尻はゆずる。ゆずれないときは届かない（理由を返す）。どちらも制約は破らない
- 保存・読み直しの後も変えられる。解析し直した・分割したノートは変えられない（理由を返す）
- オリジナルに戻すと割合の記録が無くなる。音程と一緒に変えても 1 つの changeset
- 記録の op を読み飛ばす（前の版のエンジン）と同じ編集リストになる
"""
import copy
import json
import os
import shutil

import numpy as np
import pytest
import soundfile as sf

from conftest import needs_model
from test_strength import DUR, SR, _notes, _ok, _rows, _voice

pytestmark = [needs_model]

TOL = 1e-6
SHIFT = [0.0, 0.05, -0.04, 0.06, 0.03]
# 隙間（休み）を挟んで並ぶ 2 つのノート（ゆずる・届かないを作る）: ガイドは前のノートが早く終わり、
# 後のノートが早く始まる
GAP_TAKE = [(0.5, 0.80, 60), (0.95, 1.3, 62), (1.3, 1.75, 64), (2.3, 2.8, 67)]
GAP_GUIDE = [(0.5, 0.70, 60), (0.75, 1.3, 62), (1.3, 1.75, 64), (2.36, 2.8, 67)]
GAP_DUR = 3.5


def _gap_voice(notes, transpose=0.0, seed=0, vib_hz=5.5, vib_c=30.0):
    n = int(SR * GAP_DUR)
    y = np.zeros(n)
    for st, en, midi in notes:
        a, b = int(st * SR), int(en * SR)
        t = np.arange(b - a) / SR
        f0 = 440.0 * 2 ** ((midi + transpose - 69) / 12)
        vib = vib_c * np.sin(2 * np.pi * vib_hz * t) * np.clip((t - 0.12) / 0.1, 0, 1)
        ph = 2 * np.pi * np.cumsum(f0 * 2 ** (vib / 1200)) / SR
        s = sum((0.6 ** k) * np.sin((k + 1) * ph) for k in range(8))
        env = np.minimum(1, np.minimum(t / 0.01, (en - st - t) / 0.01))
        y[a:b] += 0.25 * s * env
    y += np.random.default_rng(seed).normal(scale=1e-4, size=n)
    return y.astype("float32")


def _seed(tmp_path_factory, take_y, guide_y, name):
    from vocal_engine.project import Project
    base = tmp_path_factory.mktemp(name)
    take, guide = str(base / "take.wav"), str(base / "guide.wav")
    sf.write(take, take_y, SR, subtype="FLOAT")
    sf.write(guide, guide_y, SR, subtype="FLOAT")
    p = Project.open(take, guide, project_dir=str(base / "seed"), memo=False)
    p.analyze()
    return take, guide, p.dir


@pytest.fixture(scope="module")
def seed_t(tmp_path_factory):
    return _seed(tmp_path_factory, _voice(), _voice(0.5, 2, 4.5, 60.0, SHIFT), "ratio")


@pytest.fixture(scope="module")
def seed_gap(tmp_path_factory):
    return _seed(tmp_path_factory, _gap_voice(GAP_TAKE), _gap_voice(GAP_GUIDE, 0.5, 2, 4.5, 60.0), "gap")


def _copy(seed, d):
    from vocal_engine.project import Project
    take, guide, d0 = seed
    shutil.copytree(os.path.join(d0, "cache"), str(d / "cache"))
    p = Project.open(take, guide, project_dir=str(d), memo=False)
    p.analyze()
    return p


@pytest.fixture
def proj(seed_t, tmp_path):
    from vocal_engine import mcp_server as S
    p = _copy(seed_t, tmp_path / "p")
    S._state["project"] = p
    yield p
    S._state["project"] = None


@pytest.fixture
def proj_gap(seed_gap, tmp_path):
    from vocal_engine import mcp_server as S
    p = _copy(seed_gap, tmp_path / "p")
    S._state["project"] = p
    yield p
    S._state["project"] = None


def _grid(p):
    return np.linspace(0.0, float(p.duration_sec), 8001)


def _pos(p):
    """素材の秒の細かい格子での編集後の秒（時間の対応を密に比べる）。"""
    src, out = p.time_map()
    return np.interp(_grid(p), src, out)


def _at(p, src, side):
    from vocal_engine.project import timing as TM
    s, o = p.time_map()
    return TM.TimeMap(np.asarray(s), np.asarray(o)).at(float(src), "left" if side == "l" else "right")


def _nodes(p, nid):
    from vocal_engine.project import strength as SG
    return SG.timing_nodes(p, nid)


def _ratio(p, s, ids=None, **kw):
    from vocal_engine.project import timing as TM
    return TM.apply_plan(p, TM.plan_guide(p, ids), timing=s, **kw)


def _timing(p, ids=None):
    from vocal_engine import mcp_server as S
    return {r["note"]: r["timing"] for r in _ok(S.get_correction_strength(note_ids=ids))["notes"]}


def _sane(p):
    """制約: 時間の対応が逆戻りしない（並びが入れ替わらない）、ノートは 20 ms 以上。"""
    src, out = p.time_map()
    assert np.all(np.diff(np.asarray(out)) >= -1e-9)
    for n in _notes(p):
        assert p.edited_sec(n.end_sec) - p.edited_sec(n.start_sec) >= 0.02 - 1e-6, n.id


# ---------------------------------------------------------------- 同じ割合 = 今までの x
@pytest.mark.parametrize("s", [0.3, 0.7, 1.0])
def test_uniform_ratio_equals_x(seed_t, tmp_path, s):
    from vocal_engine.project import timing as TM
    a = _copy(seed_t, tmp_path / "a")
    TM.apply_plan(a, TM.plan_guide(a), x=s)
    b = _copy(seed_t, tmp_path / "b")
    cs, info = _ratio(b, s)
    assert cs is not None and sorted(info["timing_ratio"]["notes"]) == sorted(n.id for n in _notes(b))
    assert np.max(np.abs(_pos(a) - _pos(b))) < TOL
    assert np.max(np.abs(_pos(a) - _grid(a))) > 0.02 * s          # 実際に動いている


def test_changing_every_note_equals_x(seed_t, tmp_path):
    """100% で合わせた後、全部を 30% にすると、最初から x = 0.3 で合わせたのと同じ。"""
    from vocal_engine.project import timing as TM
    a = _copy(seed_t, tmp_path / "a")
    TM.apply_plan(a, TM.plan_guide(a), x=0.3)
    b = _copy(seed_t, tmp_path / "b")
    _ratio(b, 1.0)
    _ratio(b, 0.3)
    assert np.max(np.abs(_pos(a) - _pos(b))) < TOL
    from vocal_engine.project import strength as SG
    assert {r["timing"] for r in SG.states(b).values()} == {0.3}


# ---------------------------------------------------------------- 1 つのノート
@pytest.mark.parametrize("k", [1, 2])     # 1: 頭が基準点（anchor）、2: 頭が補間（interp）・休みの手前の尻
def test_one_note_moves_only_it_and_the_connected_tail(proj, k):
    from vocal_engine import mcp_server as S
    _ok(S.set_correction_strength(timing=1.0))
    ns = _notes(proj)
    n, prev = ns[k], ns[k - 1]
    nodes = _nodes(proj, n.id)
    if k == 2:
        assert {d["role"] for d in nodes} >= {"interp", "end"}
    assert prev.end_sec == pytest.approx(n.start_sec)             # 接している前のノート
    before = _pos(proj)
    at = {(d["src"], d["side"]): _at(proj, d["src"], d["side"]) for d in nodes}
    heads = {m.id: proj.edited_sec(m.start_sec) for m in ns}
    pl = _ok(S.plan_edit(op="guide", note_ids=[n.id]))
    tr = pl["timing_ratio"]
    assert [c["note"] for c in tr["changed"]] == [n.id] and tr["changed"][0]["timing"] == 1.0
    assert [r["note"] for r in tr["neighbours"]] == [prev.id]
    assert tr["messages"]["neighbours"] == "接している前のノートの長さも変わります（1 ノート）"
    assert tr["unreachable"] == [] and tr["messages"]["unreachable"] is None
    r = _ok(S.apply_plan(pl["plan_id"], timing=0.3))
    assert r["timing"] == 0.3 and r["timing_ratio"]["notes"] == [n.id]
    assert r["timing_ratio"]["neighbours"] == [prev.id]
    # 節 = 0% の位置 + 0.3 × d（100% の位置 − 0.7 × d）
    for d in nodes:
        assert _at(proj, d["src"], d["side"]) == pytest.approx(at[(d["src"], d["side"])] - 0.7 * d["d"], abs=TOL)
    # 動いたのは、前のノートの頭から次のノートの頭まで
    nxt = next(m for m in ns if m.start_sec >= n.end_sec - 1e-9)
    g = _grid(proj)
    moved = g[np.abs(_pos(proj) - before) > TOL]
    assert len(moved) and moved.min() > prev.start_sec and moved.max() < nxt.start_sec
    for m in ns:
        if m.id != n.id:
            assert proj.edited_sec(m.start_sec) == pytest.approx(heads[m.id], abs=TOL)
    t = _timing(proj)
    assert t[n.id] == 0.3 and all(v == 1.0 for i, v in t.items() if i != n.id)
    _sane(proj)


def test_ratio_round_trip_returns_to_the_same_place(proj):
    from vocal_engine import mcp_server as S
    _ok(S.set_correction_strength(timing=0.7))
    p07 = _pos(proj)
    ns = _notes(proj)
    _ok(S.set_correction_strength(note_ids=[ns[1].id, ns[3].id], timing=0.3))
    p03 = _pos(proj)
    assert np.max(np.abs(p03 - p07)) > 0.005
    g = _ok(S.get_correction_strength())
    assert g["summary"]["timing"]["mixed"] is True
    _ok(S.set_correction_strength(note_ids=[ns[1].id, ns[3].id], timing=0.7))
    assert np.max(np.abs(_pos(proj) - p07)) < TOL
    _ok(S.set_correction_strength(note_ids=[ns[1].id, ns[3].id], timing=0.3))
    assert np.max(np.abs(_pos(proj) - p03)) < TOL
    # 同じ割合をもう一度当てても何も変えない
    _ok(S.set_correction_strength(note_ids=[ns[1].id, ns[3].id], timing=0.3))
    assert np.max(np.abs(_pos(proj) - p03)) < TOL


# ---------------------------------------------------------------- 取り消し・当て直し・保存
def test_one_undo_restores_positions_and_ratio(proj):
    from vocal_engine import mcp_server as S
    _ok(S.set_correction_strength(timing=1.0))
    before, edits, n_cs = _pos(proj), [e.id for e in proj.edits], len(proj.changesets)
    n = _notes(proj)[1]
    r = _ok(S.set_correction_strength(note_ids=[n.id], timing=0.2))
    assert len(proj.changesets) == n_cs + 1
    _ok(S.undo(changeset_id=r["changeset"]))
    assert [e.id for e in proj.edits] == edits
    assert np.max(np.abs(_pos(proj) - before)) < TOL
    assert set(_timing(proj).values()) == {1.0}


def test_popup_replaces_keeps_one_history_entry_and_preview_matches(proj):
    """ポップアップのスライダー（同じ計画に replaces で当て直す）: 取り消し 1 回ぶん。プレビューの節の式
    （今の秒 + base + move × s）が、確定した位置と同じ。"""
    from vocal_engine import mcp_server as S
    _ok(S.set_correction_strength(timing=1.0))
    base, live = _pos(proj), len([c for c in proj.changesets if not c.undone])
    n = _notes(proj)[2]
    pl = _ok(S.plan_edit(op="guide", note_ids=[n.id]))
    with open(pl["path"], encoding="utf-8") as f:
        knots = json.load(f)["timing_ratio"]["knots"]
    assert knots and all(len(k) == 5 for k in knots)
    prev = None
    for s in (0.5, 0.2, 0.9):
        r = _ok(S.apply_plan(pl["plan_id"], timing=s, replaces=prev))
        assert not r["replanned"]
        prev = r["changeset"]
        for src, cur, b, m, side in knots:
            assert _at(proj, src, side) == pytest.approx(cur + b + m * s, abs=2e-6)
    assert len([c for c in proj.changesets if not c.undone]) == live + 1
    assert _timing(proj)[n.id] == 0.9
    _ok(S.undo(changeset_id=prev))
    assert np.max(np.abs(_pos(proj) - base)) < TOL


def test_save_and_reopen_keeps_it_changeable(proj):
    from vocal_engine import mcp_server as S
    from vocal_engine.project import Project
    _ok(S.set_correction_strength(timing=1.0))
    full = _pos(proj)
    n = _notes(proj)[1]
    _ok(S.set_correction_strength(note_ids=[n.id], timing=0.3))
    proj.save()
    q = Project.open(proj.take["path"], proj.guide["path"], project_dir=proj.dir, memo=False)
    q.analyze()
    S._state["project"] = q
    assert _timing(q)[n.id] == 0.3
    r = _ok(S.set_correction_strength(note_ids=[n.id], timing=1.0))
    assert r["changeset"] and not r["skipped"]["stale"]
    assert np.max(np.abs(_pos(q) - full)) < TOL


# ---------------------------------------------------------------- 手で直したノート
def test_manual_note_is_skipped_unless_included(proj):
    from vocal_engine import mcp_server as S
    from vocal_engine.project import timing as TM
    _ok(S.set_correction_strength(timing=1.0))
    ns = _notes(proj)
    n = ns[3]
    TM.apply_plan(proj, TM.plan_move(proj, [n.id]), 0.02, author="human")      # 手で 20 ms 遅らせた
    rows = _rows(_ok(S.get_correction_strength()))
    manual = sorted(i for i, r in rows.items() if r["timing_manual"])
    assert n.id in manual
    heads = {i: proj.edited_sec(next(m for m in ns if m.id == i).start_sec) for i in manual}
    r = _ok(S.set_correction_strength(timing=0.3))
    assert r["skipped"]["manual"] == manual
    assert r["timing_ratio"]["excluded_manual"] == manual
    for i in manual:
        assert proj.edited_sec(next(m for m in ns if m.id == i).start_sec) == pytest.approx(heads[i], abs=TOL)
    rows = _rows(_ok(S.get_correction_strength()))
    assert all(rows[i]["timing"] == 1.0 and rows[i]["timing_manual"] for i in manual)   # 手の印は残る
    assert all(rows[i]["timing"] == 0.3 for i in rows if i not in manual)
    # 含めると、割合の差 × d だけ動く（手の 20 ms は残る）
    d = next(x["d"] for x in _nodes(proj, n.id) if x["src"] == pytest.approx(n.start_sec) and x["side"] == "r")
    h = proj.edited_sec(n.start_sec)
    pl = _ok(S.plan_edit(op="guide", note_ids=[n.id], include_manual=True))
    assert [c["note"] for c in pl["timing_ratio"]["changed"]] == [n.id]
    assert pl["timing_ratio"]["changed"][0]["manual"] is True
    _ok(S.apply_plan(pl["plan_id"], timing=0.3))                  # include_manual は計画の値を使う
    assert proj.edited_sec(n.start_sec) == pytest.approx(h - 0.7 * d, abs=TOL)
    assert _timing(proj)[n.id] == 0.3
    _sane(proj)


# ---------------------------------------------------------------- ゆずる・届かない
def test_detached_previous_tail_yields(proj_gap):
    """全部 0% から、隙間を挟んだ後のノートだけ 100%: 頭が前のノートの尻を追い越すので、前の尻がゆずる。
    戻すと前のノートも元の長さに戻る。"""
    from vocal_engine import mcp_server as S
    ns = _notes(proj_gap)
    a, b = ns[0], ns[1]
    assert a.end_sec < b.start_sec - 0.05                         # 隙間がある
    _ok(S.set_correction_strength(timing=0.0))
    zero = _pos(proj_gap)
    assert np.max(np.abs(zero - _grid(proj_gap))) < TOL
    a_tail = proj_gap.edited_sec(a.end_sec)
    pl = _ok(S.plan_edit(op="guide", note_ids=[b.id]))
    assert [r["note"] for r in pl["timing_ratio"]["neighbours"]] == [a.id]
    assert pl["timing_ratio"]["unreachable"] == []
    r = _ok(S.apply_plan(pl["plan_id"], timing=1.0))
    assert r["timing_ratio"]["yielded"] == [a.id] and r["timing_ratio"]["unreachable"] == []
    assert proj_gap.edited_sec(a.end_sec) < a_tail - 0.02         # 前のノートが短くなった
    assert proj_gap.edited_sec(a.start_sec) == pytest.approx(a.start_sec, abs=TOL)
    assert any("y" in x for x in _nodes(proj_gap, a.id))          # ゆずった量を前のノートの記録に残す
    t = _timing(proj_gap)
    assert t[a.id] == 0.0 and t[b.id] == 1.0
    _sane(proj_gap)
    _ok(S.set_correction_strength(note_ids=[b.id], timing=0.0))
    assert np.max(np.abs(_pos(proj_gap) - zero)) < TOL
    assert not any("y" in x for x in _nodes(proj_gap, a.id))


def test_unreachable_note_stops_short(proj_gap):
    """全部 100% から、隙間の前のノートだけ 0%: 尻が後のノートの頭を追い越すので、手前で止まる。"""
    from vocal_engine import mcp_server as S
    ns = _notes(proj_gap)
    a, b = ns[0], ns[1]
    _ok(S.set_correction_strength(timing=1.0))
    b_head = proj_gap.edited_sec(b.start_sec)
    pl = _ok(S.plan_edit(op="guide", note_ids=[a.id]))
    ur = pl["timing_ratio"]["unreachable"]
    assert [u["note"] for u in ur] == [a.id] and ur[0]["blocker"] == b.id and ur[0]["reason"]
    assert pl["timing_ratio"]["messages"]["unreachable"] == "1 ノートは届きません"
    r = _ok(S.apply_plan(pl["plan_id"], timing=0.0))
    assert [u["note"] for u in r["timing_ratio"]["unreachable"]] == [a.id]
    assert proj_gap.edited_sec(b.start_sec) == pytest.approx(b_head, abs=TOL)   # 後のノートは動かない
    assert _timing(proj_gap)[a.id] == 0.0
    _sane(proj_gap)


# ---------------------------------------------------------------- 変えられないノート
def test_reanalysis_makes_timing_unchangeable(proj):
    from vocal_engine import mcp_server as S
    _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=1.0))
    gf = proj.guide_f0
    gf.f0[:] = np.asarray(gf.f0) * 1.001                          # ガイドを解析し直した
    rows = _ok(S.get_correction_strength())["notes"]
    assert rows and all(r["timing_state"] == "stale" and r["timing_reason"] for r in rows)
    pl = _ok(S.plan_edit(op="guide"))
    assert {u["note"] for u in pl["timing_ratio"]["unchangeable"]} == {r["note"] for r in rows}
    before, edits = _pos(proj), [e.id for e in proj.edits]
    r = _ok(S.set_correction_strength(timing=0.3))
    assert r.get("changeset") is None and r["skipped"]["stale"] == sorted(x["note"] for x in rows)
    assert [e.id for e in proj.edits] == edits and np.max(np.abs(_pos(proj) - before)) < TOL
    r = _ok(S.set_correction_strength(timing=0.3, restart=True))
    assert r["changeset"]
    assert all(x["timing_state"] == "ok" and x["timing"] == pytest.approx(0.3) for x in r["notes"])


def test_split_note_alone_is_unchangeable(proj):
    from vocal_engine import mcp_server as S
    _ok(S.set_correction_strength(timing=1.0))
    ns = _notes(proj)
    n = ns[2]
    _ok(S.split_note((n.start_sec + n.end_sec) / 2, note_id=n.id, author="human"))
    rows = _rows(_ok(S.get_correction_strength()))
    assert rows[n.id]["timing_state"] == "stale" and rows[n.id]["timing_reason"]
    assert all(rows[m.id]["timing_state"] == "ok" for m in ns if m.id != n.id)
    head = proj.edited_sec(n.start_sec)
    pl = _ok(S.plan_edit(op="guide"))
    assert [u["note"] for u in pl["timing_ratio"]["unchangeable"]] == [n.id]
    r = _ok(S.set_correction_strength(timing=0.3))
    assert r["skipped"]["stale"] == [n.id]
    assert proj.edited_sec(n.start_sec) == pytest.approx(head, abs=TOL)
    rows = _rows(_ok(S.get_correction_strength()))
    assert all(rows[m.id]["timing"] == 0.3 for m in ns if m.id != n.id)
    _sane(proj)


def test_reset_to_original_voids_the_ratio_and_holds_the_boundary(proj):
    """オリジナルに戻したノートは記録が無くなる（0%）。戻した境目（次のノートの頭）は、次のノートの割合を
    変えても動かさない（届かない）。"""
    from vocal_engine import mcp_server as S
    from vocal_engine.project import timing_ratio as TR
    _ok(S.set_correction_strength(timing=1.0))
    ns = _notes(proj)
    a, b = ns[0], ns[1]
    _ok(S.reset_to_original(note_ids=[a.id]))
    rows = _rows(_ok(S.get_correction_strength(note_ids=[a.id, b.id])))
    assert rows[a.id]["timing_state"] == "none"
    assert proj.edited_sec(a.end_sec) == pytest.approx(a.end_sec, abs=TOL)
    assert rows[b.id]["timing_manual"]                            # 頭を戻されたので手で直した扱い
    pl = _ok(S.plan_edit(op="guide", note_ids=[b.id], include_manual=True))
    ur = pl["timing_ratio"]["unreachable"]
    assert [u["note"] for u in ur] == [b.id] and ur[0]["reason"] == TR.HOLD_REASON
    _ok(S.apply_plan(pl["plan_id"], timing=0.3))
    assert proj.edited_sec(b.start_sec) == pytest.approx(b.start_sec, abs=TOL)
    _sane(proj)


# ---------------------------------------------------------------- 音程と一緒・前の版
def test_pitch_and_timing_in_one_changeset(proj):
    from vocal_engine import mcp_server as S
    _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=1.0))
    before, n_cs = _pos(proj), len(proj.changesets)
    r = _ok(S.set_correction_strength(center=0.3, timing=0.3))
    assert len(proj.changesets) == n_cs + 1
    rows = _rows(_ok(S.get_correction_strength()))
    assert all(x["timing"] == 0.3 for x in rows.values())
    assert all(x["center"] == pytest.approx(0.3) for x in rows.values() if x["state"] == "ok")
    _ok(S.undo(changeset_id=r["changeset"]))
    assert np.max(np.abs(_pos(proj) - before)) < TOL
    rows = _rows(_ok(S.get_correction_strength()))
    assert all(x["timing"] == 1.0 for x in rows.values())


def test_older_engine_reads_the_same_edits(proj_gap):
    """記録の op（recipe・y・hold）を読み飛ばしても、同じ編集リストになる（前の版のエンジンが開ける）。"""
    from vocal_engine import mcp_server as S
    from vocal_engine.project.store import _live_edits
    ns = _notes(proj_gap)
    _ok(S.set_correction_strength(timing=0.0))
    _ok(S.set_correction_strength(note_ids=[ns[1].id], timing=1.0))          # ゆずる
    _ok(S.set_correction_strength(note_ids=[ns[2].id], timing=0.6))
    _ok(S.reset_to_original(note_ids=[ns[3].id]))
    css = [copy.deepcopy(c) for c in proj_gap.changesets if not c.undone]
    assert any(n.get("y") for c in css for op in c.ops if op.get("op") == "recipe"
               for n in op.get("timing_nodes") or [])
    for c in css:
        c.ops = [op for op in c.ops if op.get("op") in ("add", "remove")]
    key = lambda es: [(e.id, e.kind, json.dumps(e.params, sort_keys=True)) for e in es]
    assert key(_live_edits(css)) == key(proj_gap.edits)


def test_bad_arguments_are_errors(proj):
    from vocal_engine import mcp_server as S
    pl = _ok(S.plan_edit(op="guide"))
    assert S.apply_plan(pl["plan_id"], x=0.5, timing=0.3).get("ok") is False
    assert S.apply_plan(pl["plan_id"], timing=1.5).get("ok") is False
    assert S.set_correction_strength(timing=-0.1).get("ok") is False
    n = _notes(proj)[1]
    mv = _ok(S.plan_edit(op="move", note_id=n.id))
    assert S.apply_plan(mv["plan_id"], timing=0.3).get("ok") is False
