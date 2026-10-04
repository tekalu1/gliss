# -*- coding: utf-8 -*-
"""解析の形（F0・音符・音素・ガイド対応付け）。"""
import numpy as np
import pytest

from conftest import needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


def test_f0_shape_and_hop(f0_take):
    f0r, x, sr = f0_take
    n_expect = int(np.floor(len(x) / sr / 0.010)) + 1
    assert f0r.n_frames == n_expect
    assert f0r.hop_s == 0.010
    assert f0r.f0.shape == f0r.confidence.shape == f0r.voiced.shape == f0r.rms_db.shape
    assert f0r.estimator == "rmvpe"
    # V/UV は「F0 が出ている ∧ RMS > -55 dBFS」
    assert np.all(f0r.f0[~f0r.voiced] == 0)
    assert 0.2 < float(np.mean(f0r.voiced)) < 0.95
    v = f0r.f0[f0r.voiced]
    assert v.min() > 50 and v.max() < 1100


def test_f0_json_roundtrip(f0_take):
    from vocal_engine.analysis.f0 import F0Result
    f0r, _, _ = f0_take
    back = F0Result.from_json(f0r.to_json())
    assert back.n_frames == f0r.n_frames
    assert np.allclose(back.f0, f0r.f0, atol=1e-3)
    assert np.array_equal(back.voiced, f0r.voiced)


def test_summarize_has_no_raw_arrays(f0_take):
    from vocal_engine.analysis.f0 import summarize
    f0r, _, _ = f0_take
    s = summarize(f0r.f0, f0r.voiced, f0r.confidence)
    assert set(s) >= {"n_frames", "n_voiced", "median_hz", "median_note", "iqr_semitones"}
    for v in s.values():
        assert not isinstance(v, (list, tuple, np.ndarray)), "MCP には生の配列を返さない"


def test_notes_cover_timeline_without_overlap(f0_take):
    from vocal_engine.analysis.notes import segment_notes
    f0r, x, sr = f0_take
    notes = segment_notes(f0r, source="take")
    assert notes, "音符のかたまりが 1 つも出ないのはおかしい"
    for a, b in zip(notes[:-1], notes[1:]):
        assert a.end_sec <= b.start_sec + 1e-6, "範囲が重なっている"
    assert notes[0].start_sec == 0.0
    assert abs(notes[-1].end_sec - f0r.n_frames * f0r.hop_s) < 0.05
    pitched = [n for n in notes if n.kind == "note"]
    assert pitched
    for n in pitched:
        assert n.pitch_hz and n.note_name and n.source == "take"
        assert n.text is None                       # 歌詞は set_lyrics で後から付く
        assert 0.0 <= n.confidence <= 1.0
        assert n.label in ("sung", "short", "voiced_unstable")
        assert n.duration_sec >= 0.05
    assert {n.kind for n in notes} <= {"note", "unvoiced", "breath", "silence"}


def test_notes_min_length_and_no_vibrato_split(f0_take):
    """ビブラート程度（±50 セント）では切れないこと。"""
    from vocal_engine.analysis.f0 import F0Result
    from vocal_engine.analysis.notes import segment_notes
    n = 300
    t = np.arange(n) * 0.010
    f0 = 220.0 * 2 ** (0.5 * np.sin(2 * np.pi * 5.5 * t) / 12.0)   # ±50 セント / 5.5 Hz
    r = F0Result(f0=f0, confidence=np.ones(n), voiced=np.ones(n, bool),
                 rms_db=np.full(n, -12.0))
    notes = [x for x in segment_notes(r) if x.kind == "note"]
    assert len(notes) == 1, "ビブラートで音符が割れている: %d" % len(notes)
    assert notes[0].label == "sung"


def test_notes_split_on_semitone_step():
    from vocal_engine.analysis.f0 import F0Result
    from vocal_engine.analysis.notes import segment_notes
    n = 400
    f0 = np.where(np.arange(n) < 200, 220.0, 220.0 * 2 ** (3 / 12.0))
    r = F0Result(f0=f0, confidence=np.ones(n), voiced=np.ones(n, bool),
                 rms_db=np.full(n, -12.0))
    notes = [x for x in segment_notes(r) if x.kind == "note"]
    assert len(notes) == 2
    assert abs(notes[0].end_sec - 2.0) < 0.06
    assert notes[1].pitch_midi - notes[0].pitch_midi == pytest.approx(3.0, abs=0.1)


def test_phonemes_supported_but_need_lyrics():
    """段階2 から音素は対応済み。ただし歌詞が要る（プロジェクト無しなら理由を返す）。"""
    from vocal_engine.analysis.phonemes import get_phonemes
    r = get_phonemes()
    assert r["supported"] is True
    assert r["phonemes"] == []
    assert "HubertFA" in r["aligner"]
    assert r["model_found"] is True, "重みの置き場に hubertfa/ が無い: %s" % r["download"]


def test_alignment_and_deviations(project):
    """C と C2 は同じ歌詞の別テイク（約 4〜5 半音違う）。"""
    project.analyze()
    al = project.alignment
    assert al is not None
    s = al.summary()
    assert s["n_points"] > 10
    # 同じタイムラインから切ったクリップなので、全体のずれは小さいはず
    assert abs(s["offset_median_ms"]) < 200
    devs, meta = project.deviations()
    assert devs
    cents = [abs(d.pitch_cents) for d in devs if d.pitch_cents is not None]
    assert cents
    med = float(np.median(cents))
    assert 300 < med < 700, "C と C2 の音程差（4〜5 半音）が出ていない: %.0f c" % med
    for d in devs:
        assert 0.0 <= d.confidence <= 1.0
