# -*- coding: utf-8 -*-
"""ノートの変わり目のなだらかさ・鉛筆・カット（REPORT-tools.md）。

- なだらかさ: 0 で段差 / 自動で段差が消える / 切り離しでは効かない / 窓の外は変わらない /
  ノートを動かしたときの結果 = 画面のプレビューの式（線形）/ 値は project.json に残る
- 鉛筆: 描いた範囲が描いた値になる / 範囲外は不変 / 書き出しで範囲外のサンプルが一致 /
  無声には描けない / 描き直し / オリジナルに戻す
- カット: 分割・結合 / 音は変わらない / 解析し直しても分割が残る / 分割後の各操作
"""
import json
import os
import shutil

import numpy as np
import pytest
import soundfile as sf

import materials as M
from conftest import GUIDE, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model, pytest.mark.usefixtures("rmvpe_f0")]   # ノートの ID・区切りは RMVPE の解析のもの

LYRICS = M.text("C.lyrics")


@pytest.fixture
def plain(tmp_path):
    from vocal_engine.project import Project
    p = Project.open(TAKE, GUIDE, project_dir=str(tmp_path / "plain"))
    p.analyze()
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)


@pytest.fixture(scope="module")
def lyr_dir(tmp_path_factory):
    from vocal_engine.project import Project
    base = tmp_path_factory.mktemp("lyr")
    p = Project.open(TAKE, GUIDE, project_dir=str(base / "seed"), lyrics=LYRICS,
                     guide_lyrics=LYRICS)
    p.analyze()
    return p.dir


@pytest.fixture
def lyr(lyr_dir, tmp_path):
    from vocal_engine.project import Project
    d = tmp_path / "lyr"
    shutil.copytree(os.path.join(lyr_dir, "cache"), str(d / "cache"))
    p = Project.open(TAKE, GUIDE, project_dir=str(d), lyrics=LYRICS, guide_lyrics=LYRICS)
    p.analyze()
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)


def _mcp(p):
    from vocal_engine import mcp_server as m
    m._state["project"] = p
    m._invalidate_renderer()
    return m


def _offsets(p):
    """(フレームの秒, 基本の段のずらし量, 層を当てたずらし量)。画面の曲線と同じ。"""
    from vocal_engine.project.pitch import pitch_model
    from vocal_engine.view.export_data import cents_offset
    base, segs, lay, trs = pitch_model(p)
    t = p.take_f0.times
    return t, cents_offset(base, t), cents_offset(segs, t), lay, trs


def _tr(p, a, b):
    from vocal_engine.project.pitch import transitions
    return next(t for t in transitions(p) if t.a == a and t.b == b)


def _shift(p, nid, cents):
    from vocal_engine.project.model import Target
    p.apply_edits([{"kind": "pitch_shift", "target": Target.note(nid), "params": {"cents": cents}}],
                  author="human")


def _voiced(p):
    f0r = p.take_f0
    return np.asarray(f0r.voiced, dtype=bool) & (np.asarray(f0r.f0) > 0)


def _export_diff(p, tmp_path):
    from vocal_engine.render.export import export_wav
    out = str(tmp_path / "out.wav")
    r = export_wav(p, path=out)
    a, sr = sf.read(TAKE, dtype="int32", always_2d=True)
    b, _ = sf.read(out, dtype="int32", always_2d=True)
    assert a.shape == b.shape
    d = np.flatnonzero(np.any(a != b, axis=1))
    return (None if not len(d) else (d[0] / sr, d[-1] / sr)), r


# ================================================================ なだらかさ
def test_transition_zero_is_a_step_and_auto_removes_it(plain):
    """n014｜n015（接続、170 ms と 310 ms）の n014 を +100 セント。0 = 段差、自動 = 段差が消える。"""
    m = _mcp(plain)
    _shift(plain, "n014", 100)
    tr = _tr(plain, "n014", "n015")
    assert tr.value == 0.5 and 0.03 <= tr.auto_sec <= 0.25 and tr.auto_how == "measured"
    t, base, off, _, _ = _offsets(plain)
    k = int(round(tr.tb / plain.take_f0.hop_s))
    assert abs(base[k - 1] - 100) < 1e-6 and abs(base[k]) < 1e-6   # 基本の段は段差
    # 自動: 境目をまたぐフレームの差が段差（100）よりずっと小さい
    assert np.max(np.abs(np.diff(off[k - 12:k + 12]))) < 30
    assert 5 < off[k] < 95
    # 0 = 段差（今までと同じ）
    r = m.set_transition(value=0.0, note_ids=["n014"], author="human")   # 両側の境目
    assert r["ok"] and r["changeset"] and r["pairs"] == 2
    t, base, off, _, _ = _offsets(plain)
    assert abs(off[k - 1] - 100) < 1e-6 and abs(off[k]) < 1e-6
    assert np.allclose(off, base)
    # 右端 = もっとゆっくり（窓が広い）
    m.set_transition(value=1.0, note_a="n014", note_b="n015", author="human")
    tr1 = _tr(plain, "n014", "n015")
    assert tr1.hl + tr1.hr > tr.hl + tr.hr


def test_transition_keeps_original_shape_outside_the_window(plain):
    """窓の外は基本の段のまま。窓の中も「原音 + 補間した移動量」（形はそのまま）。"""
    _shift(plain, "n004", -80)
    t, base, off, lay, trs = _offsets(plain)
    wins = [(x.lo, x.hi) for x in trs if x.active]
    assert wins, "n004 の両側の境目が効いている"
    inside = np.zeros(len(t), dtype=bool)
    for a, b in wins:
        inside |= (t >= a - 1e-9) & (t <= b + 1e-9)
    assert np.allclose(off[~inside], base[~inside])
    # 窓の中の値は基本の段の最小〜最大の間（行き過ぎない）
    assert off[inside].min() >= min(base.min(), 0) - 1e-6
    assert off[inside].max() <= max(base.max(), 0) + 1e-6


def test_transition_not_applied_on_detached_boundary(plain):
    """切り離し（n007｜n009、息を挟む）には効かない。接続を切った境目も段差のまま。"""
    m = _mcp(plain)
    from vocal_engine.project.pitch import transitions
    assert not any(t.a == "n007" and t.b == "n009" for t in transitions(plain))
    m.set_connection("n005", "n006", False, author="human")
    _shift(plain, "n005", 100)
    t, base, off, _, trs = _offsets(plain)
    assert not any(x.a == "n005" and x.b == "n006" for x in trs)
    k = int(round(plain.note("n006").start_sec / plain.take_f0.hop_s))
    assert abs(off[k - 1] - 100) < 1e-6 and abs(off[k]) < 1e-6      # n005｜n006 は段差のまま
    kk = int(round(plain.note("n005").start_sec / plain.take_f0.hop_s))
    assert 0 < off[kk] < 100                                        # n004｜n005（接続）は効く


def test_transition_drag_is_linear_like_the_preview(plain):
    """ノートを d 動かした結果 = 動かす前 + d·H + keep·(S − H)·dΔ（画面のプレビューの式）。"""
    _shift(plain, "n003", 50)
    t, base0, off0, lay0, trs0 = _offsets(plain)
    _shift(plain, "n004", 120)
    t, base1, off1, lay1, trs1 = _offsets(plain)
    n4 = plain.note("n004")
    H4 = ((t >= n4.start_sec) & (t < n4.end_sec)).astype(float)
    pred = off0 + 120 * H4
    for tr in trs0:
        da = 120 if tr.a == "n004" else 0
        db = 120 if tr.b == "n004" else 0
        if da == db:
            continue
        for i, ti in enumerate(t):
            if tr.lo <= ti <= tr.hi and not (tr.ta < ti < tr.tb):
                h = 1.0 if ti >= tr.tb else 0.0
                pred[i] += (tr.s_of(ti) - h) * (db - da)
    v = _voiced(plain)
    assert np.max(np.abs(pred[v] - off1[v])) < 1e-6


def test_transition_value_persists_and_popup_replaces(plain):
    """値は project.json に残る。replaces で当て直すと changeset は 1 つだけ残る。"""
    m = _mcp(plain)
    _shift(plain, "n010", 100)
    r1 = m.set_transition(value=0.2, note_ids=["n010"], author="human")
    r2 = m.set_transition(value=0.8, note_ids=["n010"], replaces=r1["changeset"], author="human")
    assert r2["pairs"] == 2                              # n009｜n010 と n010｜n011
    live = [c for c in plain.changesets if not c.undone]
    assert r1["changeset"] not in [c.id for c in live]
    with open(plain.json_path, encoding="utf-8") as f:
        d = json.load(f)
    tr = [e for e in d["edits"] if e["kind"] == "transition"]
    assert {(e["params"]["a"], e["params"]["b"]) for e in tr} == {("n009", "n010"),
                                                                  ("n010", "n011")}
    assert all(abs(e["params"]["value"] - 0.8) < 1e-9 for e in tr)
    # 自動に戻す = 上書きを外す
    m.set_transition(value=0.5, note_ids=["n010"], author="human")
    assert not [e for e in plain.edits if e.kind == "transition"]
    rows = {(r["a"], r["b"]): r for r in m.list_connections()["connections"]}
    assert rows[("n010", "n011")]["transition"]["auto"] is True
    assert rows[("n010", "n011")]["transition"]["smoothing"] is True
    assert "transition" not in rows[("n007", "n009")]


def test_unedited_boundaries_stay_original(plain, tmp_path):
    """ピッチを動かしていなければ、つなぎは何もしない（書き出し = 元のまま）。"""
    diff, r = _export_diff(plain, tmp_path)
    assert diff is None and r["replaced_spans_sec"] == []


# ================================================================ 鉛筆
def _draw(plain, t0, t1, midi_fn, step=0.01):
    m = _mcp(plain)
    ts = np.arange(t0, t1 + 1e-9, step)
    return m.set_pitch_curve(points=[[float(t), float(midi_fn(t))] for t in ts], mode="draw",
                             author="human")


def test_draw_sets_the_drawn_value_and_keeps_the_rest(plain, tmp_path):
    n = plain.note("n005")
    t0, t1 = n.start_sec + 0.03, n.end_sec - 0.03
    before = _offsets(plain)[2]
    r = _draw(plain, t0, t1, lambda t: 66.0 + 2.0 * (t - t0))
    assert r["ok"], r
    t, base, off, lay, _ = _offsets(plain)
    f0r = plain.take_f0
    midi = 69 + 12 * np.log2(np.maximum(np.asarray(f0r.f0), 1e-9) / 440.0)
    v = _voiced(plain)
    a, b = r["start_sec"], r["end_sec"]
    inr = v & (t >= a - 1e-9) & (t <= b + 1e-9)
    assert inr.sum() >= 5
    got = midi[inr] + off[inr] / 100.0
    want = 66.0 + 2.0 * (t[inr] - t0)
    assert np.max(np.abs(got - want)) < 1e-3               # 描いた値になる
    ramp = 0.04
    out = (t < a - ramp - 1e-9) | (t > b + ramp + 1e-9)
    assert np.allclose(off[out], before[out])              # 範囲（＋つなぎ）の外は変わらない
    # 書き出し: 違うサンプルは描いた範囲の近く（差し替えた窓の中）だけ
    diff, rr = _export_diff(plain, tmp_path)
    assert diff is not None
    spans = rr["replaced_spans_sec"]
    assert any(s0 - 1e-3 <= diff[0] and diff[1] <= s1 + 1e-3 for s0, s1 in spans)
    assert diff[0] > a - ramp - 0.6 and diff[1] < b + ramp + 0.6


def test_draw_moves_with_a_later_note_drag(plain):
    """描いたあとにノートを動かすと、描いた線ごと動く（画面のドラッグと同じ）。"""
    n = plain.note("n005")
    r = _draw(plain, n.start_sec + 0.03, n.end_sec - 0.03, lambda t: 65.0)
    t, _, off0, _, _ = _offsets(plain)
    _shift(plain, "n005", 30)
    t, _, off1, _, _ = _offsets(plain)
    inr = _voiced(plain) & (t >= r["start_sec"]) & (t <= r["end_sec"])
    assert np.allclose(off1[inr] - off0[inr], 30.0, atol=1e-6)


def test_draw_on_unvoiced_only_is_refused(plain):
    m = _mcp(plain)
    a = next(n for n in plain.take_notes if n.kind != "note" and n.end_sec - n.start_sec > 0.1)
    r = m.set_pitch_curve(points=[[a.start_sec + 0.02, 60.0], [a.end_sec - 0.02, 62.0]],
                          mode="draw", author="human")
    assert not r["ok"] and "無声" in r["error"]


def test_redraw_replaces_and_reset_trims(plain):
    m = _mcp(plain)
    n5, n6 = plain.note("n005"), plain.note("n006")
    _draw(plain, n5.start_sec + 0.05, n5.end_sec - 0.05, lambda t: 64.0)
    r2 = _draw(plain, n5.start_sec + 0.02, n6.end_sec - 0.02, lambda t: 67.0)
    assert r2["replaced"] == 1
    draws = [e for e in plain.edits if e.kind == "pitch_draw"]
    assert len(draws) == 1
    # n006 だけ戻す → n005 にかかる部分の鉛筆は残る
    rr = m.reset_to_original(note_ids=["n006"], author="human")
    assert rr["ok"]
    draws = [e for e in plain.edits if e.kind == "pitch_draw"]
    assert len(draws) == 1
    assert draws[0].target.end_sec <= n6.start_sec + 1e-6
    t, base, off, _, _ = _offsets(plain)
    inn6 = (t >= n6.start_sec + 0.05) & (t < n6.end_sec)
    assert np.allclose(off[inn6], 0.0, atol=1e-6)


# ================================================================ カット
def test_split_and_merge(plain, tmp_path):
    m = _mcp(plain)
    n = plain.note("n006")
    t = round(0.5 * (n.start_sec + n.end_sec), 3)
    before_ids = [x.id for x in plain.take_notes]
    r = m.split_note(sec=t, author="human")
    assert r["ok"], r
    rid = r["right"]
    assert rid == "n006@%d" % round(t * 1000)
    ids = [x.id for x in plain.take_notes]
    assert len(ids) == len(before_ids) + 1 and ids.index(rid) == ids.index("n006") + 1
    left, right = plain.note("n006"), plain.note(rid)
    assert abs(left.end_sec - t) < 1e-9 and abs(right.start_sec - t) < 1e-9
    assert right.kind == "note" and right.pitch_midi is not None
    rows = {(x["a"], x["b"]): x for x in m.list_connections()["connections"]}
    assert rows[("n006", rid)]["connected"] is True           # 新しい境目は接続
    assert ("n006", "n007") not in rows and (rid, "n007") in rows
    # 分割だけでは音は変わらない
    diff, _ = _export_diff(plain, tmp_path)
    assert diff is None
    # 結合（分割した境目 = split を外す）
    r2 = m.merge_notes("n006", rid, author="human")
    assert r2["ok"] and r2["removed_split"] is True
    assert [x.id for x in plain.take_notes] == before_ids
    assert not [e for e in plain.edits if e.kind in ("split", "merge")]


def test_split_keeps_existing_edits_and_merge_analysis_boundary(plain):
    """ピッチを動かしたノートを分割しても音は同じ。解析でできた境目も結合できる。"""
    m = _mcp(plain)
    _shift(plain, "n015", 70)
    t0, _, off0, _, _ = _offsets(plain)
    n = plain.note("n015")
    m.split_note(sec=round(0.5 * (n.start_sec + n.end_sec), 3), author="human")
    t1, _, off1, _, _ = _offsets(plain)
    v = _voiced(plain)
    assert np.allclose(off0[v], off1[v], atol=1e-6)
    # 片方だけ戻す → 戻した側だけ 0、もう片方は +70 のまま
    right = [x for x in plain.take_notes if x.id.startswith("n015@")][0]
    assert m.reset_to_original(note_ids=[right.id], author="human")["ok"]
    t2, base2, _, _, _ = _offsets(plain)
    inr = (t2 >= right.start_sec) & (t2 < right.end_sec)
    inl = (t2 >= n.start_sec) & (t2 < right.start_sec)
    assert np.allclose(base2[inr], 0.0) and np.allclose(base2[inl], 70.0)
    notes = {x.id: x for x in plain.take_notes}
    before = len(notes)
    r = m.merge_notes("n004", "n005", author="human")
    assert r["ok"] and r["removed_split"] is False
    assert len(plain.take_notes) == before - 1
    merged = plain.note("n004")
    assert abs(merged.end_sec - notes["n005"].end_sec) < 1e-9
    with pytest.raises(Exception):
        plain.note("n005")


def test_split_survives_reanalysis(plain):
    m = _mcp(plain)
    n = plain.note("n010")
    t = round(n.start_sec + 0.05, 3)
    r = m.split_note(sec=t, author="human")
    rid = r["right"]
    plain.analyze(force=True)
    ids = [x.id for x in plain.take_notes]
    assert rid in ids
    assert abs(plain.note(rid).start_sec - t) < 1e-9
    # project.json から開き直しても残る
    from vocal_engine.project import Project
    q = Project(plain.dir).load()
    assert rid in [x.id for x in q.take_notes]


def test_split_notes_work_with_every_edit(plain):
    """分割したノートに、ピッチ・タイミング（端・移動）・なだらかさ・ガイドに合わせるが効く。"""
    from vocal_engine.project import timing as TM
    m = _mcp(plain)
    n = plain.note("n006")
    t = round(0.5 * (n.start_sec + n.end_sec), 3)
    rid = m.split_note(sec=t, author="human")["right"]
    # ピッチ
    assert m.shift_pitch(cents=100, note_id=rid, author="human")["ok"]
    # なだらかさ（新しい境目）
    tr = _tr(plain, "n006", rid)
    assert abs(tr.ta - t) < 1e-9
    r = m.set_transition(value=0.9, note_a="n006", note_b=rid, author="human")
    assert r["ok"] and r["pairs"] == 1
    tt, base, off, _, _ = _offsets(plain)
    k = int(round(t / plain.take_f0.hop_s))
    assert 0 < off[k] < 100
    # タイミング: 右端のドラッグ（接続）→ 隣（n007）の頭が一緒に動く
    rr = m.stretch(ratio=1.2, note_id=rid, author="human")
    assert rr["ok"], rr
    tm = TM.current_map(plain)
    assert tm.at(plain.note(rid).end_sec, "left") > plain.note(rid).end_sec + 0.005
    # 移動
    assert m.move_note(ms=-10, note_id="n006", author="human")["ok"]
    # 計画（画面の端のドラッグ）
    pl = m.plan_edit(op="edge", note_id=rid, side="start")
    assert pl["ok"] and pl["info"]["connected"] is True
    # ガイドに合わせる
    g = m.correct_to_guide(note_ids=["n006", rid], pitch_strength=1.0, timing_strength=0.0,
                           author="human")
    assert g["ok"] and g["pitch_notes"] >= 1
    # オリジナルに戻す
    assert m.reset_to_original(note_ids=[rid], author="human")["ok"]


def test_split_snaps_to_phoneme_boundary(lyr):
    m = _mcp(lyr)
    res = lyr.phonemes("take")
    from vocal_engine.project import timing as TM
    ns = TM.pitched_notes(lyr)
    b = next(b for b in res.boundaries
             if any(n.start_sec + 0.03 < b.time_sec < n.end_sec - 0.03 for n in ns))
    r = m.split_note(sec=b.time_sec + 0.012, snap_ms=20, author="human")
    assert r["ok"] and r["snapped"] is True
    assert abs(r["sec"] - round(b.time_sec, 4)) < 1e-9


# ================================================================ レビューで直したもの（2026-09-23）
def test_split_and_reset_keep_the_edit_order(plain):
    """分割（ノート対象 → 範囲対象）・オリジナルに戻す（範囲を切る・鉛筆を切る）で付け替えた編集は
    編集リストの元の位置に入る。末尾に回ると、鉛筆より前のピッチ編集が「後から入った編集」になって
    描いた線に足される（100 セントずれた）。"""
    m = _mcp(plain)
    n = plain.note("n015")
    _shift(plain, "n015", 100)
    m.set_transition(value=0.0, note_ids=["n014", "n015", "n016"], author="human")   # つなぎの影響を除く
    mid = 0.5 * (n.start_sec + n.end_sec)
    _draw(plain, mid - 0.05, mid + 0.05, lambda t: n.pitch_midi + 0.5)
    t, _, off0, _, _ = _offsets(plain)
    r = m.split_note(mid + 0.08, author="human")
    assert r["ok"], r
    t, _, off1, _, _ = _offsets(plain)
    assert np.allclose(off1, off0, atol=1e-6)
    kinds = [e.kind for e in plain.edits if e.kind in ("pitch_shift", "pitch_draw")]
    assert kinds == ["pitch_shift", "pitch_draw"]
    # 範囲のピッチ編集が n015〜n016 にまたがる → n016 だけ戻しても n015（鉛筆の下）は変わらない
    for _ in range(4):
        m.undo()
    assert not plain.edits
    n16 = plain.note("n016")
    m.shift_pitch(100, start_sec=n.start_sec, end_sec=n16.end_sec, author="human")
    m.set_transition(value=0.0, note_ids=["n015", "n016"], author="human")
    _draw(plain, mid - 0.05, mid + 0.05, lambda t: n.pitch_midi + 0.5)
    _shift(plain, "n015", 40)                          # 鉛筆の後のドラッグ（描いた線ごと動く）
    t, _, off0, _, _ = _offsets(plain)
    assert m.reset_to_original(note_ids=["n016"], author="human")["ok"]
    t, _, off1, _, _ = _offsets(plain)
    in15 = (t >= n.start_sec) & (t < n.end_sec)
    assert np.allclose(off1[in15], off0[in15], atol=1e-6)


def test_reset_leaves_no_draw_ramp_in_the_reset_note(plain):
    """オリジナルに戻したノートには、残した鉛筆の端のつなぎ（40 ms）も入らない。
    鉛筆がノートの手前で終わっていて、つなぎだけが入り込んでいるときも。"""
    m = _mcp(plain)
    n5, n6 = plain.note("n005"), plain.note("n006")
    _draw(plain, n5.start_sec + 0.02, n6.end_sec - 0.02, lambda t: 67.0)
    assert m.reset_to_original(note_ids=["n006"], author="human")["ok"]
    t, _, off, _, _ = _offsets(plain)
    in6 = (t >= n6.start_sec) & (t < n6.end_sec)
    assert np.allclose(off[in6], 0.0, atol=1e-6)
    d = [e for e in plain.edits if e.kind == "pitch_draw"]
    assert len(d) == 1 and d[0].params.get("ramp_r_sec") == 0.0
    # 手前で終わる鉛筆（範囲は重ならず、つなぎだけ入る）
    for _ in range(2):
        m.undo()
    _draw(plain, n5.start_sec + 0.02, n5.end_sec - 0.01, lambda t: 67.0)
    t, _, off, _, _ = _offsets(plain)
    assert np.any(np.abs(off[in6]) > 1.0)               # 戻す前は n006 の頭に入り込んでいる
    assert m.reset_to_original(note_ids=["n006"], author="human")["ok"]
    t, _, off, _, _ = _offsets(plain)
    assert np.allclose(off[in6], 0.0, atol=1e-6)


def test_layer_pieces_are_rendered_as_one_segment(plain):
    """ノート 1 つのピッチ移動とその両側のつなぎは、1 つの区間として再合成する
    （窓の端で切ったまま別々に合成して 20 ms で重ねると、つなぎ目が 4 つ増えていた）。"""
    from vocal_engine.project.pitch import layered_segments
    from vocal_engine.render.pipeline import build_renderer
    _shift(plain, "n015", 100)
    segs = [s for s in layered_segments(plain) if not s.is_identity()]
    assert len(segs) == 1
    s = segs[0]
    n = plain.note("n015")
    assert s.start_sec < n.start_sec and s.end_sec > n.end_sec     # 両側の窓を含む
    r = build_renderer(plain, backend="psola")
    y, info = r.render_range(2.5, 4.0, layered_segments(plain))
    assert info["edited_chunks"] == 1 and len(y) == int(round(1.5 * plain.take["sr"]))


def test_layer_fill_keeps_silence_position_and_legacy_move(plain):
    """窓の中の埋め（編集の無いところ）が、無音の挿入の時刻をまたがない・旧式の move を吸収できる。"""
    from vocal_engine.project.model import Target
    from vocal_engine.project.pitch import layered_segments
    from vocal_engine.render.pipeline import build_renderer
    sr = plain.take["sr"]
    _shift(plain, "n015", 100)                         # n014｜n015 の窓が n014 の尻（〜40 ms）に入る
    tr = _tr(plain, "n014", "n015")
    ts = round(tr.ta - 0.5 * tr.hl, 4)
    plain.apply_edits([{"kind": "silence", "target": Target.range(ts, ts),
                        "params": {"sec": 0.05}}], author="human")
    r = build_renderer(plain, backend="psola")
    y, info = r.render_range(2.5, 4.0, layered_segments(plain))
    k0, k1 = int(round((ts - 2.5 + 0.012) * sr)), int(round((ts - 2.5 + 0.05 - 0.012) * sr))
    assert np.max(np.abs(y[k0:k1])) < 1e-9              # 無音はその時刻に出る
    # 旧式の move（ノートを 30 ms 後ろへ）＋ピッチ: 長さは変わらない（前の隙間が吸収する）
    plain.undo()
    plain.apply_edits([{"kind": "move", "target": Target.note("n009"), "params": {"ms": 30}},
                       {"kind": "pitch_shift", "target": Target.note("n009"),
                        "params": {"cents": 100}}], author="human")
    y, info = r.render_range(2.0, 3.0, layered_segments(plain))
    assert len(y) == sr and not info["warnings"]


def test_undo_of_split_with_later_edit_on_the_piece_is_refused(plain):
    """分割の changeset だけを取り消すと、後の編集の対象（右の片）が無くなる → 断る（再合成が止まらない）。"""
    m = _mcp(plain)
    n = plain.note("n015")
    r = m.split_note(0.5 * (n.start_sec + n.end_sec), author="human")
    _shift(plain, r["right"], 100)
    u = m.undo(changeset_id=r["changeset"])
    assert not u["ok"] and r["right"] in u["error"]
    assert any(e.kind == "split" for e in plain.edits)
    from vocal_engine.project.pitch import layered_segments
    layered_segments(plain)                             # 止まらない
    assert m.undo()["ok"] and m.undo()["ok"]            # 後ろから順になら取り消せる


def test_split_near_an_edge_keeps_the_existing_transition(plain):
    """ノートの端の近くで分割しても、既存の境目のつなぎの窓（ノートの長さの半分の上限）は変わらない
    （分割した片の長さで上限を測っていたので、頭から 50 ms で分割して 33 セント変わっていた）。"""
    m = _mcp(plain)
    _shift(plain, "n015", 100)
    n = plain.note("n015")
    t, _, off0, _, _ = _offsets(plain)
    assert m.split_note(n.start_sec + 0.05, author="human")["ok"]
    t, _, off1, _, trs = _offsets(plain)
    assert np.max(np.abs(off1 - off0)) < 1e-6
    # 分割した境目そのものは、分けた片の長さの半分まで
    sp = next(x for x in trs if x.a == "n015" and x.b.startswith("n015@"))
    assert sp.max_hl <= 0.5 * 0.05 + 1e-6


def test_guide_results_carry_correspondence(plain):
    """issue #53: plan_edit(guide) と correct_to_guide の結果に、ノートごとの対応と理由が入る。
    画面が読む計画の JSON にも同じ中身（notes）がある。"""
    import json
    m = _mcp(plain)
    pl = m.plan_edit(op="guide")
    assert pl["ok"]
    rows = pl["correspondence"]
    from vocal_engine.project import timing as TM
    assert {r["note"] for r in rows} == {n.id for n in TM.pitched_notes(plain)}
    for r in rows:
        assert r["timing"] in ("anchor", "interp", None)
        assert isinstance(r["guide"], list) and isinstance(r["pitch"], bool)
        if r["timing"] is None or not r["confirmed"]:
            assert r["reason"]
    assert pl["info"]["timing_possible"] is True
    assert pl["info"]["timing_interp_notes"] >= 1
    with open(pl["path"], encoding="utf-8") as f:
        data = json.load(f)
    assert [r["note"] for r in data["notes"]] == [r["note"] for r in rows]
    assert all("confirmed" in pr for pr in data["pairs"])
    g = m.correct_to_guide(pitch_strength=0.0, timing_strength=1.0, author="human")
    assert g["ok"] and g["timing_possible"] is True
    assert g["timing_notes"] == pl["info"]["timing_notes"]
    assert len(g["correspondence"]) == len(rows)
    assert "timing_message" not in g
