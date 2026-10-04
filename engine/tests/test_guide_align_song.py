# -*- coding: utf-8 -*-
"""同じ時間軸で録った曲（素材 S3。`docs/testing.md`）で、ガイドとの対応を譜面（MIDI）を正解にして測る。

- 帯の制約（`align.estimate_timeline`）: テイクが曲の一部しか歌っていないファイルでも、DTW が別のフレーズに
  写らない（以前は歌った音程ノートの半分以上が 250 ms を超えて外れ、中央値で 2 秒ずれた）
- 譜面ガイド（`score_guide`）: ガイドの WAV が息・囁きで音程を持たない区間でも、譜面のノートに対応する

正解: 譜面の音符（`S3.midi_bpm` で読み、`S3.midi_start_sec` だけずらす）のうち、テイクのノートと最も重なる音符。
テイクのノートの対応先（ガイドのノート）がその音符と重なり（±50 ms）、高さが 1 半音以内なら「正しい対応」。
"""
import os

import numpy as np
import pytest

import materials as MT
from conftest import needs_material, needs_model

pytestmark = [needs_material("S3_M2", "S3_GM", "S3_AS", "S3_MID"), needs_model]
POS_TOL = 0.25


def _truth(guide_symbol):
    from vocal_engine import score_guide as SG, score_import
    score = score_import.read_score(MT.clip("S3_MID"))
    tr = score["tracks"][MT.data("S3.midi_track")[guide_symbol]]
    notes = SG.retime(tr["notes"], 120.0 / float(MT.data("S3.midi_bpm")))
    s = float(MT.data("S3.midi_start_sec"))
    return [(n["start_sec"] + s, n["end_sec"] + s, float(n["pitch"])) for n in notes]


def _ov(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0)


def coverage(p, midi, pitch_target=None):
    """正しい対応のノートの割合（正解のあるテイクの音程ノートのうち）。"""
    from vocal_engine.project import timing as TM
    if pitch_target is None:
        _, pitch_target, _ = TM.note_correspondence(p)
    n_truth = ok = 0
    for n in TM.pitched_notes(p):
        best = max(midi, key=lambda m: _ov(n.start_sec, n.end_sec, m[0], m[1]))
        if _ov(n.start_sec, n.end_sec, best[0], best[1]) <= 0:
            near = [m for m in midi if max(0.0, max(n.start_sec, m[0]) - min(n.end_sec, m[1])) <= POS_TOL]
            if not near:
                continue
            best = min(near, key=lambda m: max(0.0, max(n.start_sec, m[0]) - min(n.end_sec, m[1])))
        n_truth += 1
        g = pitch_target.get(n.id)
        if g is not None and _ov(g.start_sec, g.end_sec, best[0] - 0.05, best[1] + 0.05) > 0 \
                and g.pitch_midi is not None and abs(g.pitch_midi - best[2]) <= 1.0:
            ok += 1
    return ok / max(1, n_truth)


def dtw_far(p):
    """歌った音程ノートの真ん中を DTW で写した位置が、同じ時間軸から 250 ms を超えて外れた数。"""
    from vocal_engine.project import timing as TM
    mids = np.array([(n.start_sec + n.end_sec) / 2 for n in TM.pitched_notes(p)])
    return int((np.abs(p.alignment.to_guide(mids) - mids) > POS_TOL).sum())


def test_band_keeps_dtw_on_the_timeline(tmp_path):
    """曲の一部だけ歌ったテイク: 同じ時間軸と判定して帯を掛け、DTW の外れが無くなる。対応の被覆率も保つ。"""
    from vocal_engine.project import Project
    from vocal_engine.project import timing as TM
    exp = MT.data("S3.expect")["M2"]
    p = Project.open(MT.clip("S3_M2"), MT.clip("S3_GM"), project_dir=str(tmp_path / "m2"))
    p.analyze()
    info = p.alignment.info
    assert info["timeline"]["same"] and abs(info["timeline"]["offset_sec"]) <= 0.05, info
    assert dtw_far(p) <= exp["dtw_far_max"] < exp["dtw_far_before"]
    midi = _truth("S3_GM")
    assert coverage(p, midi) >= exp["coverage_min"]
    # 同じ時間軸の判定（guide_timing）に頼らず DTW だけで対応を取っても、外れない
    orig = p.guide_to_take
    p.guide_to_take = lambda: ((lambda t: p.alignment.to_take(t)), {"basis": "dtw", "same_timeline": False})
    try:
        _, pt, _ = TM.note_correspondence(p)
    finally:
        p.guide_to_take = orig
    assert coverage(p, midi, pt) >= exp["dtw_only_coverage_min"]


def test_score_guide_covers_breathy_guide(tmp_path):
    """息の多いガイド（音程の取れない区間がある）の代わりに譜面ガイド: 時間の頭とトラックを当て、被覆率が上がる。"""
    from vocal_engine import score_guide as SG, score_import
    from vocal_engine.project import Project
    exp = MT.data("S3.expect")["AS"]
    p = Project.open(MT.clip("S3_AS"), MT.clip("S3_GA"), project_dir=str(tmp_path / "wav"))
    p.analyze()
    midi = _truth("S3_GA")
    before = coverage(p, midi)
    assert abs(before - exp["wav_coverage"]) < 0.05           # 素材の前提（ガイドの WAV では低い）
    score = score_import.read_score(MT.clip("S3_MID"))
    notes, start, y, info = SG.build(score, p.take_f0, 0.0, p.onsets("take"), p.guide["duration_sec"],
                                     bpm=MT.data("S3.midi_bpm"))
    assert info["track"]["index"] == exp["score_track"]
    assert abs(info["fit"]["pitch_start_sec"] - MT.data("S3.midi_start_sec")) <= 0.04, info["fit"]
    w = str(tmp_path / "score-guide.wav")
    SG.write(w, y, info["played"], info)
    q = Project.open(MT.clip("S3_AS"), w, project_dir=str(tmp_path / "score"))
    q.analyze()
    after = coverage(q, midi)
    assert after >= exp["score_coverage_min"] > before


@needs_material("S3_WK")
def test_sparse_take_keeps_timeline(tmp_path):
    """掛け声だけのテイク（166 秒に数秒）: 全体のずれが壊れず（以前は −22 秒・組 0）、タイムライン基準で組が取れる。"""
    from vocal_engine.project import Project
    exp = MT.data("S3.expect")["WK"]
    p = Project.open(MT.clip("S3_WK"), MT.clip("S3_GM"), project_dir=str(tmp_path / "wk"))
    p.analyze()
    tl = p.alignment.info["timeline"]
    assert (tl["same"] or tl["loose"]) and abs(tl["offset_sec"]) <= 0.05, tl
    gt = p.guide_timing()
    assert gt.basis == "timeline" and abs(gt.measured_offset_sec) <= 0.05
    assert len(gt.pairs) >= exp["pairs_min"]
