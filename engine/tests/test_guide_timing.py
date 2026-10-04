# -*- coding: utf-8 -*-
"""「ガイドに合わせる」のタイミング 100% が**ガイドの発音の頭に揃う**こと（issue #12）。

答えの分かっているガイドを作って確かめる: テイク C のノートをいくつか前後に動かして
書き出し（PSOLA）、頭に 0.25 秒の無音を足したものをガイドにする。テイクのどの時刻 t も、
ガイドでは W(t) + 0.25 にある（W = 動かしたときの時間写像）。「揃う」= 補正後に T(t) = W(t)
（全体のずれ 0.25 秒は曲の置き場所なので動かさない）。

直す前の規則（DTW でテイクに写したガイドの位置へ合わせる）では、写した位置がテイク自身の
リズムに沿うので、T(t) はほとんど動かないか、DTW の誤差の分だけ崩れる。
"""
import os
import shutil

import numpy as np
import pytest
import soundfile as sf

import materials as M
from conftest import CLIP_E, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]

PAD_SEC = 0.25
MOVES = {"n003": 0.06, "n006": -0.05, "n011": 0.045, "n014": -0.04}


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    """(ガイドの WAV, W の (src, out))。"""
    from vocal_engine.project import Project
    from vocal_engine.project import timing as TM
    from vocal_engine.render.export import export_wav
    base = tmp_path_factory.mktemp("synth")
    p = Project.open(TAKE, project_dir=str(base / "warp"))
    p.analyze()
    for nid, x in MOVES.items():
        plan = TM.plan_move(p, [nid])
        assert plan.x_lo <= x <= plan.x_hi
        TM.apply_plan(p, plan, x)
    src, out = p.time_map()
    w = str(base / "warped.wav")
    export_wav(p, path=w)
    y, sr = sf.read(w, always_2d=True)
    pad = int(round(PAD_SEC * sr))
    g = np.vstack([np.zeros((pad, y.shape[1]), dtype=y.dtype), y])
    guide = str(base / "guide.wav")
    sf.write(guide, g, sr, subtype="FLOAT")
    return guide, (np.asarray(src), np.asarray(out))


def _open(tmp_path, guide, name="p"):
    from vocal_engine.project import Project
    p = Project.open(TAKE, guide, project_dir=str(tmp_path / name))
    p.analyze()
    return p


def _eval_points(p):
    """測る時刻: テイクの発音の頭（エンジンと**別の方式**で拾う。onsets_ref.py）。"""
    from onsets_ref import detect_onsets_energy
    x, sr = p.audio("take")
    t = detect_onsets_energy(x, sr)
    return t[(t > 0.3) & (t < 3.4)]


def test_global_offset_is_found_and_not_moved(synth, tmp_path):
    """全体のずれ（頭の 0.25 秒）は「曲の置き場所」として見つけ、それ自体は動かさない。"""
    guide, _ = synth
    p = _open(tmp_path, guide)
    gt = p.guide_timing()
    assert abs(gt.offset_sec + PAD_SEC) < 0.02, gt.summary()
    assert len(gt.pairs) >= 5
    shutil.rmtree(p.dir, ignore_errors=True)


def test_timing_100_lands_on_the_guide_onsets(synth, tmp_path):
    """100% で、テイクの発音の頭が答えの位置 W(t) に揃う。直す前より必ず近い。"""
    from vocal_engine.project import timing as TM
    guide, (src, out) = synth
    p = _open(tmp_path, guide)
    ev = _eval_points(p)
    W = lambda t: float(np.interp(t, src, out))          # noqa: E731
    before = np.array([(t - W(t)) * 1000.0 for t in ev])
    plan = TM.plan_guide(p)
    assert plan.info["reach"] == 1.0
    TM.apply_plan(p, plan, 1.0)
    tm = TM.current_map(p)
    after = np.array([(tm.at(t) - W(t)) * 1000.0 for t in ev])
    b, a = np.abs(before), np.abs(after)
    assert np.median(b) > 25.0                              # 動かしたぶんが測れている
    assert np.median(a) <= 10.0, (np.median(a), after.round(1))
    assert np.mean(a <= 10.0) >= 0.6, after.round(1)
    # 揃えられなかった頭（対応に自信が無くて動かさなかった所）は元より悪くならない
    worse = int(np.sum(a > b + 10.0))
    assert worse <= 1, list(zip(ev.round(3), before.round(1), after.round(1)))
    # 目標に届いた頭は、ちょうど「ガイドの頭 + 全体のずれ」にある
    for e in plan.timing:
        if e["reached"]:
            assert abs(tm.at(e["take_sec"]) - e["target_sec"]) < 2e-4
    shutil.rmtree(p.dir, ignore_errors=True)


def test_estimated_reading_uses_note_timing_and_partial_confirmation_uses_phonemes(synth, tmp_path):
    from vocal_engine.project import timing as TM

    guide, _ = synth
    p = _open(tmp_path, guide, name="lyrics-scope")
    text = M.text("C.lyrics")
    entry = {"start_sec": None, "end_sec": None, "text": text,
             "origin": "estimated", "estimate": {"reading": text, "confidence": 0.5}}
    p.change_lyrics([entry], source="take")
    p.set_lyrics(text, source="guide")
    p.analyze_phonemes("take")
    p.analyze_phonemes("guide")
    assert p.guide_timing().source == "syllables"

    estimated = TM.plan_guide(p)
    assert estimated.timing
    assert all(e["take_sec"] == round(estimated.st.knots[
        estimated.st.note_knots[e["note"]][0]].src, 4) for e in estimated.timing)
    assert not TM._phoneme_list(p, confirmed_only=True)
    assert not any(pc.mode == "keep" and pc.note for pc in estimated.st.pieces)

    internal = next(ph.syllable_index for ph in p.phonemes("take").phonemes
                    if ph.syllable_index is not None and any(
                        n.kind == "note" and n.start_sec + 1e-5 < ph.end_sec < n.end_sec - 1e-5
                        for n in p.take_notes))
    p.change_lyrics([dict(entry, confirmed_syllables=[internal])], source="take")
    p.analyze_phonemes("take")
    confirmed = TM._phoneme_list(p, confirmed_only=True)
    assert confirmed and {ph.syllable_index for ph in confirmed} == {internal}
    partial = TM.plan_guide(p)
    assert len([k for k in partial.st.knots if k.role == "inner"]) > len(
        [k for k in estimated.st.knots if k.role == "inner"])

    p.change_lyrics([dict(entry, origin="confirmed")], source="take")
    p.analyze_phonemes("take")
    assert len(TM._phoneme_list(p, confirmed_only=True)) > len(confirmed)
    shutil.rmtree(p.dir, ignore_errors=True)


def test_offset_only_guide_moves_nothing(tmp_path):
    """ガイド = テイクを 0.25 秒遅らせただけ → リズムは同じなので 100% でも 1 つも動かない。"""
    from vocal_engine.project import timing as TM
    x, sr = sf.read(TAKE, always_2d=True)
    g = np.vstack([np.zeros((int(PAD_SEC * sr), x.shape[1]), dtype=x.dtype), x])
    guide = str(tmp_path / "shifted.wav")
    sf.write(guide, g, sr, subtype="FLOAT")
    p = _open(tmp_path, guide)
    plan = TM.plan_guide(p)
    assert abs(plan.info["offset_ms"] + PAD_SEC * 1000.0) < 10.0
    assert float(np.max(np.abs(plan.d))) < 0.006, plan.timing   # 検出の揺れ（5 ms の刻み）まで
    shutil.rmtree(p.dir, ignore_errors=True)


def test_unrelated_guide_moves_little(tmp_path):
    """中身の違うガイド（別の曲の一節）では、対応に自信が持てないので大半の頭を動かさない。"""
    from vocal_engine.project import timing as TM
    p = _open(tmp_path, CLIP_E)
    plan = TM.plan_guide(p)
    ev = p.onsets("take")
    assert len(plan.timing) <= max(2, 0.3 * len(ev)), plan.timing
    shutil.rmtree(p.dir, ignore_errors=True)


def test_guide_is_drawn_at_guide_time_plus_offset(synth, tmp_path):
    """画面のガイドの位置 = ガイドの時刻 + 全体のずれ（DTW で写した位置ではない）。"""
    import json
    from vocal_engine.view.export_data import export_view_data
    guide, _ = synth
    p = _open(tmp_path, guide)
    gt = p.guide_timing()
    with open(export_view_data(p)["path"], encoding="utf-8") as f:
        vd = json.load(f)
    gn = [g for g in vd["guide_notes"] if g["kind"] == "note"]
    assert gn
    for g in gn:
        assert abs(g["start_sec"] - float(gt.to_take(g["guide_start_sec"]))) < 1e-3
    shutil.rmtree(p.dir, ignore_errors=True)


# ================================================================ 基準点と補間（issue #53）
def _cover(plan):
    return {r["note"]: r["timing"] for r in plan.notes}


def test_interpolation_covers_notes_between_anchors(tmp_path):
    """基準点の間にある、発音の頭の組が無いノートも、前後の基準点に合わせて比例で動く。

    C / C2（別の歌唱）: 以前の規則（隙間を挟まない両隣だけ）では頭に目標の付かないノートがある。
    補間ではそれらも目標を持ち、100% で目標に届く。基準点の頭はそのまま（同じ目標）。"""
    from vocal_engine.project import Project
    from vocal_engine.project import timing as TM
    from conftest import GUIDE
    p = Project.open(TAKE, GUIDE, project_dir=str(tmp_path / "c"))
    p.analyze()
    old = TM.plan_guide(p, interpolate=False)
    new = TM.plan_guide(p)
    ids = {n.id for n in TM.pitched_notes(p)}
    assert {r["note"] for r in new.notes} == ids
    co, cn = _cover(old), _cover(new)
    anchored = {n for n, t in cn.items() if t == "anchor"}
    assert anchored == {n for n, t in co.items() if t == "anchor"}     # 基準点は変わらない
    gained = {n for n in ids if cn[n] and not co[n]}
    assert gained and all(cn[n] == "interp" for n in gained), (co, cn)
    assert sum(1 for t in cn.values() if t) > sum(1 for t in co.values() if t)
    assert new.info["timing_interp_notes"] == sum(1 for t in cn.values() if t == "interp")
    # 基準点の目標は以前と同じ
    assert [(e["take_sec"], e["target_sec"]) for e in new.timing] == \
        [(e["take_sec"], e["target_sec"]) for e in old.timing]
    # 補間したノートの頭は、前後の基準点の目標の間にある（比例）
    st = new.st
    for r in new.notes:
        if r["timing"] != "interp" or not r["reached"]:
            continue
        ks = st.note_knots[r["note"]][0]
        d = new.d[ks]
        lo = min(e["d_sec"] for e in new.timing)
        hi = max(e["d_sec"] for e in new.timing)
        assert lo - 1e-6 <= d <= hi + 1e-6
    shutil.rmtree(p.dir, ignore_errors=True)


def test_interpolated_plan_preview_equals_result_and_undo(synth, tmp_path):
    """ドラッグ中（計画の式 cur + d·x）＝離した後（確定した時間写像）。取り消すと元どおり。"""
    from vocal_engine.project import timing as TM
    guide, _ = synth
    p = _open(tmp_path, guide, name="preview")
    plan = TM.plan_guide(p)
    assert any(r["timing"] == "interp" for r in plan.notes)
    x = 0.6
    want = plan.new_positions(x)
    cs, _ = TM.apply_plan(p, plan, x)
    tm = TM.current_map(p)
    for k, kn in enumerate(plan.st.knots):
        if kn.role == "edge":
            continue
        assert abs(tm.at(kn.src, kn.side) - want[k]) < 2e-4, (k, kn)
    p.undo(cs.id)
    tm = TM.current_map(p)
    for kn in plan.st.knots:
        assert abs(tm.at(kn.src, kn.side) - kn.src) < 1e-6
    shutil.rmtree(p.dir, ignore_errors=True)


def test_interpolation_keeps_synthetic_accuracy(synth, tmp_path):
    """答えの分かっているガイドで、補間を入れても揃い方は落ちない（直す前より悪くなる頭は 1 つまで）。"""
    from vocal_engine.project import timing as TM
    guide, (src, out) = synth
    p = _open(tmp_path, guide, name="acc")
    ev = _eval_points(p)
    W = lambda t: float(np.interp(t, src, out))          # noqa: E731
    res = {}
    for interp in (False, True):
        cs, _ = TM.apply_plan(p, TM.plan_guide(p, interpolate=interp), 1.0)
        tm = TM.current_map(p)
        res[interp] = np.abs([(tm.at(t) - W(t)) * 1000.0 for t in ev])
        p.undo(cs.id)
    before = np.abs([(t - W(t)) * 1000.0 for t in ev])
    a = res[True]
    assert np.median(a) <= 10.0, a.round(1)
    assert int(np.sum(a > before + 10.0)) <= 1
    assert np.percentile(a, 90) <= np.percentile(res[False], 90) + 1.0
    shutil.rmtree(p.dir, ignore_errors=True)


def test_syllable_ends_are_anchors_only_with_confirmed_lyrics(synth, tmp_path):
    """テイク・ガイドとも確定の歌詞: 休みの手前の音節の終わりも基準点（届く）。推定の読みでは使わない。"""
    from vocal_engine.project import timing as TM
    guide, _ = synth
    p = _open(tmp_path, guide, name="ends")
    text = M.text("C.lyrics")
    p.set_lyrics(text, source="take")
    p.set_lyrics(text, source="guide")
    p.analyze_phonemes("take")
    p.analyze_phonemes("guide")
    assert p.guide_timing().source == "syllables"
    plan = TM.plan_guide(p)
    ends = [e for e in plan.timing if e.get("kind") == "end"]
    assert ends, plan.timing
    assert all(e["take"].endswith("-end") and e["reached"] for e in ends)
    res = p.phonemes("take")
    ends_sec = {round(s["end_sec"], 4) for s in res.syllables}
    assert all(e["take_sec"] in ends_sec for e in ends)
    entry = {"start_sec": None, "end_sec": None, "text": text,
             "origin": "estimated", "estimate": {"reading": text, "confidence": 0.5}}
    p.change_lyrics([entry], source="take")
    p.analyze_phonemes("take")
    assert not [e for e in TM.plan_guide(p).timing if e.get("kind") == "end"]
    shutil.rmtree(p.dir, ignore_errors=True)


def test_guide_without_anchors_reports_it(synth, tmp_path, monkeypatch):
    """確かな頭の組が 1 つも無いガイド（158 秒の曲のような別演奏）: タイミングは 1 つも動かさず、
    計画と MCP の結果に「合わせられない」と理由を出す。"""
    from vocal_engine.analysis import guide_timing as GT
    from vocal_engine.project import timing as TM
    guide, _ = synth
    p = _open(tmp_path, guide, name="none")
    monkeypatch.setattr(GT, "of_project", lambda project, align_lyrics=True: GT.GuideTiming(offset_sec=0.0))
    plan = TM.plan_guide(p)
    assert plan.info["timing_possible"] is False
    assert "合わせられない" in plan.info["timing_message"]
    assert float(np.max(np.abs(plan.d))) == 0.0
    assert plan.notes and all(r["timing"] is None and "1 つも無い" in r["timing_reason"]
                              for r in plan.notes)
    rows = TM.correspondence_summary(plan)
    assert all(r["reason"] for r in rows)
    shutil.rmtree(p.dir, ignore_errors=True)


def test_unrelated_guide_explains_unmatched_notes(tmp_path):
    """中身の違うガイド: 対応の無いノート・タイミングの無いノートには理由が付く。"""
    from vocal_engine.project import timing as TM
    p = _open(tmp_path, CLIP_E)
    plan = TM.plan_guide(p)
    for r in plan.notes:
        if not r["confirmed"]:
            assert r["reason"]
        if r["timing"] is None:
            assert r["timing_reason"]
    # 比例で動かすノートは、基準点より大きくは動かない（基準点の目標の間の値）
    ds = [abs(e["d_sec"]) for e in plan.timing]
    assert float(np.max(np.abs(plan.d))) <= (max(ds) if ds else 0.0) + 1e-4   # d_sec は 0.1 ms に丸め
    shutil.rmtree(p.dir, ignore_errors=True)
