# -*- coding: utf-8 -*-
"""「ガイドに合わせる」の音程の割合を後から変える（`project/strength.py`。2026-10-11 承認の段階 1）。

素材は使わない（合成の歌声。ガイドは半音の半分高く、揺れの速さと深さが違う）。解析モデルの重みだけ要る。

- 記録（recipe）が保存・読み直しで残る。割合を 0.7 → 0.3 → 0.7 と変えると最初と同じ線になる
- 後から入れた shift_pitch は、割合を変えても残る（手で直したノートは既定で変えない）
- 解析し直した（ガイドの指紋が違う）ノートは変えられない（理由を返し、restart で合わせ直せる）
- 割合の変更は 1 つの changeset（1 回の取り消しで戻る）
- 記録の op を読み飛ばす（前の版のエンジン）と同じ編集リストになる
- 割合の違う隣との境目に段差が無い。100% は今までの「Hz で寄せる」と同じ線
- オリジナルに戻すと記録も無くなる
"""
import copy
import json
import os
import shutil

import numpy as np
import pytest
import soundfile as sf

from conftest import needs_model

pytestmark = [needs_model]

SR = 44100
# (頭の秒, 長さ, MIDI)。前の 3 つと後の 2 つが、それぞれ続いた 1 つの区間になる
NOTES = [(0.5, 0.40, 60), (0.9, 0.40, 62), (1.3, 0.45, 64), (2.3, 0.5, 67), (2.8, 0.5, 65)]
DUR = 4.0


def _voice(transpose=0.0, seed=0, vib_hz=5.5, vib_c=30.0):
    n = int(SR * DUR)
    y = np.zeros(n)
    for st, d, midi in NOTES:
        a, b = int(st * SR), int((st + d) * SR)
        t = np.arange(b - a) / SR
        f0 = 440.0 * 2 ** ((midi + transpose - 69) / 12)
        vib = vib_c * np.sin(2 * np.pi * vib_hz * t) * np.clip((t - 0.12) / 0.1, 0, 1)
        ph = 2 * np.pi * np.cumsum(f0 * 2 ** (vib / 1200)) / SR
        s = sum((0.6 ** k) * np.sin((k + 1) * ph) for k in range(8))
        env = np.minimum(1, np.minimum(t / 0.03, (d - t) / 0.04))
        y[a:b] += 0.25 * s * env
    y += np.random.default_rng(seed).normal(scale=1e-4, size=n)
    return y.astype("float32")


@pytest.fixture(scope="module")
def seed(tmp_path_factory):
    """解析は 1 回だけ。テストごとにキャッシュを写して開き直す。"""
    from vocal_engine.project import Project
    base = tmp_path_factory.mktemp("strength")
    take, guide = str(base / "take.wav"), str(base / "guide.wav")
    sf.write(take, _voice(), SR, subtype="FLOAT")
    sf.write(guide, _voice(0.5, 2, 4.5, 60.0), SR, subtype="FLOAT")
    p = Project.open(take, guide, project_dir=str(base / "seed"), memo=False)
    p.analyze()
    return take, guide, p.dir


@pytest.fixture
def proj(seed, tmp_path):
    from vocal_engine import mcp_server as S
    from vocal_engine.project import Project
    take, guide, d0 = seed
    d = tmp_path / "p"
    shutil.copytree(os.path.join(d0, "cache"), str(d / "cache"))
    p = Project.open(take, guide, project_dir=str(d), memo=False)
    p.analyze()
    S._state["project"] = p
    yield p
    S._state["project"] = None
    shutil.rmtree(p.dir, ignore_errors=True)


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _view(p):
    from vocal_engine.view.export_data import export_view_data
    with open(export_view_data(p)["path"], encoding="utf-8") as f:
        f0 = json.load(f)["f0"]
    t = f0["t0_sec"] + np.arange(len(f0["take_midi"])) * f0["hop_sec"]
    nan = lambda xs: np.array([np.nan if x is None else x for x in xs], dtype="float64")
    return t, nan(f0["take_midi"]), nan(f0["take_edited_midi"])


def _notes(p):
    return [n for n in p.take_notes if n.kind == "note"]


def _rows(r):
    return {x["note"]: x for x in r["notes"]}


def _draw_points(p, nid):
    """ノートの記録が指す鉛筆の点（区切りの順）。"""
    from vocal_engine.project import strength as SG
    _, ent = SG.recipes(p)[1][nid]
    by = {e.id: e for e in p.edits}
    return [by[i].params["points"] for i in ent["edits"]]


def _same_points(a, b, tol=1e-6):
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert np.asarray(x).shape == np.asarray(y).shape
        assert np.max(np.abs(np.asarray(x) - np.asarray(y))) <= tol


# ---------------------------------------------------------------- 式だけ（解析なし）
def test_equal_ratios_reproduce_hz_blend():
    """音程 = 線の形 = p なら、今までの「Hz で p だけ寄せる」（`_guide_blend_midi`）と同じ値。"""
    from vocal_engine.project import strength as SG
    from vocal_engine.project import timing as TM
    rng = np.random.default_rng(0)
    m0 = 60 + np.cumsum(rng.normal(scale=0.05, size=40))
    m1 = m0 + 0.5 + 0.3 * np.sin(np.arange(40) / 3.0)
    runs = [[(100 + i, m0[i], m1[i], "a") for i in range(40)]]
    sg = SG.Seg(SG.make_segs(runs, 0.01, {})[0], 0.01)
    for pct in (0.0, 0.3, 0.7, 1.0):
        v = SG.base_values(sg, (pct, pct, float(np.mean(sg.e))))
        h0 = 440 * 2 ** ((m0 - 69) / 12)
        h1 = 440 * 2 ** ((m1 - 69) / 12)
        want = [TM._guide_blend_midi(a, b, w, pct) for a, b, w in zip(h0, h1, sg.w)]
        assert np.max(np.abs(v - np.asarray(want))) < 2e-3      # 材料は 1/1000 半音に丸める


def test_center_only_moves_note_center_not_shape():
    """線の形 0・音程 100% は、ノートの中心だけをガイドへ（揺れはテイクのまま）。"""
    from vocal_engine.project import strength as SG
    m0 = 60 + 0.3 * np.sin(np.arange(60) / 2.0)
    m1 = np.full(60, 61.0)
    sg = SG.Seg(SG.make_segs([[(i, m0[i], m1[i], "a") for i in range(60)]], 0.01, {})[0], 0.01)
    v = SG.base_values(sg, (1.0, 0.0, float(np.mean(sg.e))))
    mid = slice(10, 50)                     # 区間の端のなだらかさの外
    d = v[mid] - m0[mid]
    assert np.std(d) < 0.03                 # 揺れは残る（一定量ずらしただけ）
    assert abs(float(np.mean(v[mid])) - 61.0) < 0.05


def test_boundary_of_different_ratios_has_no_step():
    """割合の違う隣との境目は、両側 20 ms で混ぜる（段差にしない）。"""
    from vocal_engine.project import strength as SG
    hop = 0.01
    n = 80
    m0 = np.full(n, 60.0)
    m1 = np.full(n, 61.0)
    run = [(i, m0[i], m1[i], "a" if i < 40 else "b") for i in range(n)]
    note = type("N", (), {"end_sec": 0.395})
    mat = SG.Material("k", SG.make_segs([run], hop, {"a": note}), hop)
    ratios = {"a": (0.1, 0.1), "b": (0.9, 0.9)}
    a, b = SG.seg_points(mat, 0, ratios.get), SG.seg_points(mat, 1, ratios.get)
    assert a[-1] == b[0] == [0.395, a[-1][1]]          # 2 本の鉛筆は境目の同じ点で会う
    pts = a + b[1:]
    t = np.array([x[0] for x in pts])
    v = np.array([x[1] for x in pts])
    assert np.all(np.diff(t) > 0)
    gap = 0.8                               # 0.1 と 0.9 の差（1 半音の 80%）
    steps = np.abs(np.diff(v))
    assert steps.max() < 0.45 * gap         # 段差なら gap が 1 フレームで変わる
    mid = (t[1:] > 0.2) & (t[1:] < 0.6)     # 区間の端（40 ms のなだらかさ）の外
    moving = t[1:][mid & (steps > 0.02 * gap)]
    assert 0.03 - 1e-9 <= moving.max() - moving.min() + hop <= 0.06   # 30〜50 ms ほどで変わる


# ---------------------------------------------------------------- 解析した曲
def test_recipe_round_trip_and_reopen(proj):
    from vocal_engine import mcp_server as S
    from vocal_engine.project import Project
    r = _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=0.0))
    ids = [n.id for n in _notes(proj)]
    st = {x["note"]: x for x in r["strength"]}
    assert st and all(abs(st[i]["center"] - 0.7) < 1e-9 for i in ids)
    g = _ok(S.get_correction_strength())
    assert {x["note"] for x in g["notes"]} == set(ids)
    assert g["summary"]["center"]["mixed"] is False
    edits = [(e.id, e.kind, json.dumps(e.params, sort_keys=True)) for e in proj.edits]
    proj.save()
    q = Project.open(proj.take["path"], proj.guide["path"], project_dir=proj.dir, memo=False)
    q.analyze()
    assert [(e.id, e.kind, json.dumps(e.params, sort_keys=True)) for e in q.edits] == edits
    S._state["project"] = q
    g2 = _ok(S.get_correction_strength())
    assert [(x["note"], x["state"], x["center"], x["shape"]) for x in g2["notes"]] == \
        [(x["note"], x["state"], x["center"], x["shape"]) for x in g["notes"]]
    ls = _ok(S.list_notes())["notes"]
    assert all(d["guide_strength"]["center"] == 0.7 for d in ls if d["id"] in ids)


def test_ratio_change_comes_back_to_the_same_line(proj):
    """0.7 → 0.3 → 0.7 で、最初に 0.7 で当てたときと同じ鉛筆の点になる。"""
    from vocal_engine import mcp_server as S
    _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=0.0))
    ids = [n.id for n in _notes(proj)]
    first = {i: _draw_points(proj, i) for i in ids}
    _, _, e07 = _view(proj)
    r = _ok(S.set_correction_strength(center=0.3, shape=0.3))
    assert r["changeset"] and all(abs(x["center"] - 0.3) < 1e-9 for x in r["notes"])
    _, _, e03 = _view(proj)
    assert np.nanmax(np.abs(e03 - e07)) > 0.1
    _ok(S.set_correction_strength(center=0.7, shape=0.7))
    for i in ids:
        _same_points(_draw_points(proj, i), first[i])
    _, _, back = _view(proj)
    assert np.nanmax(np.abs(back - e07)) < 1e-4
    # 同じ割合をもう一度当てても何も変えない
    r = _ok(S.set_correction_strength(center=0.7, shape=0.7))
    assert r.get("changeset") is None


def test_one_undo_restores_ratio_and_line(proj):
    from vocal_engine import mcp_server as S
    _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=0.0))
    _, _, before = _view(proj)
    edits = [e.id for e in proj.edits]
    n_cs = len(proj.changesets)
    r = _ok(S.set_correction_strength(center=0.2, shape=1.0))
    assert len(proj.changesets) == n_cs + 1
    _ok(S.undo(changeset_id=r["changeset"]))
    _, _, after = _view(proj)
    assert [e.id for e in proj.edits] == edits
    assert np.nanmax(np.abs(after - before)) < 1e-6
    assert all(abs(x["center"] - 0.7) < 1e-9 for x in _ok(S.get_correction_strength())["notes"])


def test_popup_replaces_keeps_one_history_entry(proj):
    """ポップアップのスライダー（同じ計画に replaces で当て直す）は、何度動かしても取り消し 1 回ぶん。"""
    from vocal_engine import mcp_server as S
    _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=0.0))
    _, _, base = _view(proj)
    pl = _ok(S.plan_edit(op="guide"))
    prev = None
    for c in (0.5, 0.2, 0.9):
        r = _ok(S.apply_plan(pl["plan_id"], x=0.0, pitch_center=c, replaces=prev))
        assert not r["replanned"]
        prev = r["changeset"]
    live = [c for c in proj.changesets if not c.undone]
    assert sum(1 for c in proj.changesets if c.id == prev) == 1
    assert all(abs(x["center"] - 0.9) < 1e-9 and abs(x["shape"] - 0.7) < 1e-9
               for x in _ok(S.get_correction_strength())["notes"])
    _ok(S.undo(changeset_id=prev))
    _, _, back = _view(proj)
    assert np.nanmax(np.abs(back - base)) < 1e-6
    assert len([c for c in proj.changesets if not c.undone]) == len(live) - 1


def test_mixed_ratios_are_reported(proj):
    from vocal_engine import mcp_server as S
    _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=0.0))
    ns = _notes(proj)
    _ok(S.set_correction_strength(note_ids=[ns[1].id], center=0.2))
    g = _ok(S.get_correction_strength(note_ids=[n.id for n in ns[:3]]))
    assert g["summary"]["center"]["mixed"] is True
    assert g["summary"]["center"]["min"] == pytest.approx(0.2)
    assert g["summary"]["center"]["max"] == pytest.approx(0.7)
    assert g["summary"]["shape"]["mixed"] is False


def test_later_shift_survives_and_manual_is_skipped(proj):
    """後から shift_pitch したノートは既定で変えない。含めると、補正の割合だけ変えて手の 50 セントは残る。"""
    from vocal_engine import mcp_server as S
    _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=0.0))
    n = _notes(proj)[1]
    _ok(S.shift_pitch(cents=50, note_id=n.id, author="human"))
    t, _, shifted = _view(proj)
    inside = (t > n.start_sec + 0.06) & (t < n.end_sec - 0.06)
    r = _ok(S.set_correction_strength(center=0.3, shape=0.3))
    assert n.id in r["skipped"]["manual"]
    _, _, e = _view(proj)
    assert np.nanmax(np.abs(e[inside] - shifted[inside])) < 1e-6      # 手で直したノートはそのまま
    assert _rows(_ok(S.get_correction_strength()))[n.id]["center"] == pytest.approx(0.7)
    r = _ok(S.set_correction_strength(note_ids=[n.id], center=0.3, shape=0.3, include_manual=True))
    assert r["changeset"] and not r["skipped"]["manual"]
    _, _, with_shift = _view(proj)
    # 手の 50 セントを外した同じ割合の線と比べる
    S.undo(changeset_id=r["changeset"])
    sh = next(e for e in proj.edits if e.kind == "pitch_shift")
    proj.apply_changes([sh.id], [], author="human", label="外す")
    _ok(S.set_correction_strength(note_ids=[n.id], center=0.3, shape=0.3, include_manual=True))
    _, _, no_shift = _view(proj)
    d = (with_shift - no_shift)[inside]
    d = d[np.isfinite(d)]
    assert len(d) > 10 and np.max(np.abs(d - 0.5)) < 0.01


def test_reanalysis_makes_recipe_stale(proj):
    from vocal_engine import mcp_server as S
    _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=0.0))
    gf = proj.guide_f0
    gf.f0[:] = np.asarray(gf.f0) * 1.001                     # ガイドを解析し直した
    g = _ok(S.get_correction_strength())
    assert g["stale"] and all(x["state"] == "stale" for x in g["notes"])
    assert all("ガイド" in x["reason"] for x in g["notes"])
    edits = [e.id for e in proj.edits]
    r = _ok(S.set_correction_strength(center=0.3, shape=0.3))
    assert r.get("changeset") is None and r["skipped"]["stale"]
    assert [e.id for e in proj.edits] == edits
    r = _ok(S.set_correction_strength(center=0.3, shape=0.3, restart=True))
    assert r["changeset"] and all(x["state"] == "ok" and x["center"] == pytest.approx(0.3)
                                  for x in r["notes"])


def test_reset_to_original_clears_recipe(proj):
    from vocal_engine import mcp_server as S
    _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=0.0))
    n = _notes(proj)[0]
    _ok(S.reset_to_original(note_ids=[n.id]))
    st = _rows(_ok(S.get_correction_strength(note_ids=[n.id])))
    assert n.id not in st or st[n.id]["state"] == "none"
    others = _rows(_ok(S.get_correction_strength()))
    assert n.id not in others


def test_zero_removes_the_line(proj):
    from vocal_engine import mcp_server as S
    _, _, orig = _view(proj)
    _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=0.0))
    _ok(S.set_correction_strength(center=0.0, shape=0.0))
    assert not [e for e in proj.edits if e.kind == "pitch_draw"]
    assert not _ok(S.get_correction_strength())["notes"]
    _, _, e = _view(proj)
    assert np.nanmax(np.abs(e - orig)) < 1e-6


def test_older_engine_reads_the_same_edits(proj):
    """記録の op（recipe / recipe_void）を読み飛ばしても、同じ編集リストになる（前の版のエンジンが開ける）。"""
    from vocal_engine import mcp_server as S
    from vocal_engine.project.store import _live_edits
    _ok(S.correct_to_guide(pitch_strength=0.7, timing_strength=0.0))
    ns = _notes(proj)
    _ok(S.set_correction_strength(note_ids=[ns[1].id], center=0.2, shape=0.9))
    _ok(S.shift_pitch(cents=-30, note_id=ns[3].id, author="human"))
    _ok(S.set_correction_strength(center=0.4, include_manual=True))
    _ok(S.reset_to_original(note_ids=[ns[0].id]))
    css = [copy.deepcopy(c) for c in proj.changesets if not c.undone]
    assert any(op.get("op") == "recipe" for c in css for op in c.ops)
    for c in css:
        c.ops = [op for op in c.ops if op.get("op") in ("add", "remove")]
    key = lambda es: [(e.id, e.kind, json.dumps(e.params, sort_keys=True)) for e in es]
    assert key(_live_edits(css)) == key(proj.edits)


def test_mixed_ratio_boundary_is_smooth_in_the_song(proj):
    """割合の違う隣（同じ区間で続く）との境目で、補正の量が 1 フレームで跳ばない。"""
    from vocal_engine import mcp_server as S
    _ok(S.correct_to_guide(pitch_strength=0.2, timing_strength=0.0))
    t, orig, lo = _view(proj)
    _ok(S.set_correction_strength(center=1.0, shape=1.0))
    _, _, hi = _view(proj)
    ns = _notes(proj)
    _ok(S.set_correction_strength(center=0.2, shape=0.2))
    _ok(S.set_correction_strength(note_ids=[ns[1].id], center=1.0, shape=1.0))
    _, _, mixed = _view(proj)
    checked = 0
    for b in (ns[0].end_sec, ns[1].end_sec):
        w = (t > b - 0.06) & (t < b + 0.06)
        k = int(np.argmin(np.abs(t - b)))
        gap = abs((hi - orig)[k] - (lo - orig)[k])
        if not np.isfinite(gap) or gap < 0.1:
            continue
        step = lambda x: np.nanmax(np.abs(np.diff((x - orig)[w])))
        assert step(mixed) <= max(step(hi), step(lo)) + 0.45 * gap, b
        checked += 1
    assert checked >= 1


def test_full_strength_matches_previous_path(proj):
    """100% は、割合の記録を使わない今までの当て方（区間ごとの鉛筆）と同じ線。"""
    from vocal_engine.project import timing as TM
    _, _, before = _view(proj)
    plan = TM.plan_guide(proj)
    old = copy.copy(plan)
    old.strength = None
    cs, _ = TM.apply_plan(proj, old, 0.0, pitch=1.0)
    _, _, legacy = _view(proj)
    proj.undo(cs.id)
    cs, _ = TM.apply_plan(proj, plan, 0.0, pitch=1.0)
    _, _, now = _view(proj)
    assert np.nanmax(np.abs(legacy - before)) > 0.2
    assert np.nanmax(np.abs(now - legacy)) < 0.005           # 0.5 セント未満
