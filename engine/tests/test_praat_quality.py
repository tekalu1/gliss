# -*- coding: utf-8 -*-
"""Praat（praat-parselmouth）バックエンドの品質の回帰テスト（2026-09-23 から既定のバックエンド）。

固定すること:
  1. 既定が praat で、parselmouth が無い環境では自前 psola に落ちて警告が出る
  2. ピッチ編集が評価の段階の parselmouth の書き出し（聴き比べで良かった音）と同じ音になる
  3. ピッチが狙いどおりに動く（一律・曲線）、伸縮の長さが ratio どおり
  4. 編集区間と原音のつなぎ目で音量が膨らまない・痩せない、クリックが出ない
  5. 高調波間のノイズ床が評価の段階の praat より悪くならない
2 の基準の書き出しは素材から作るので、素材のフォルダの praat-reference/ に置く（無ければ skip）。
"""
import os

import numpy as np
import pytest

import materials as M
from conftest import CLIP_A, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model,
              pytest.mark.skipif(not __import__("vocal_engine.render.praat", fromlist=["x"]).available(),
                                 reason="praat-parselmouth が入っていない")]

REFERENCE = M.path("praat-reference")
CLIP_D = M.clip("D")


def _load(path):
    from vocal_engine.analysis.f0 import estimate_f0
    from vocal_engine.audio import read_mono
    x, sr = read_mono(path)
    return x, sr, estimate_f0(x=x, sr=sr)


@pytest.fixture(scope="module")
def clip_c():
    return _load(TAKE)


@pytest.fixture(scope="module")
def clip_a():
    return _load(CLIP_A)


def _renderer(clip, backend=None):
    from vocal_engine.render.pipeline import Renderer
    x, sr, f0r = clip
    return Renderer(x, sr, f0r.f0, f0r.voiced, f0r.hop_s, backend=backend), x, sr


def test_default_backend_is_praat(clip_c):
    from vocal_engine.render.base import DEFAULT_BACKEND, list_backends
    r, _, _ = _renderer(clip_c)
    assert DEFAULT_BACKEND == "praat"
    assert r.backend_name == "praat"
    info = {b["name"]: b for b in list_backends()}
    assert info["praat"]["default"] and info["praat"]["available"]
    assert "GPL" in info["praat"]["license"]
    assert not info["psola"]["default"]            # 自前は選択肢として残る


def test_falls_back_to_psola_without_parselmouth(clip_c, monkeypatch):
    from vocal_engine.render import base, praat
    monkeypatch.setattr(praat, "available", lambda: False)
    monkeypatch.setattr(base, "_warned", set())
    with pytest.warns(RuntimeWarning, match="parselmouth"):
        assert base.resolve_backend_name(None) == "psola"
    r, _, _ = _renderer(clip_c)
    assert r.backend_name == "psola"
    assert base.resolve_backend_name("psola") == "psola"      # 明示の指定はそのまま


def _gain_matched(a, b):
    """b をゲインだけ合わせて a と比べる（相関、SNR dB、ゲイン dB）。"""
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    g = float(np.dot(a, b) / np.dot(b, b))
    err = a - g * b
    snr = 10 * np.log10(np.sum(a * a) / max(np.sum(err * err), 1e-30))
    corr = float(np.dot(a, b) / np.sqrt(np.dot(a, a) * np.dot(b, b)))
    return corr, snr, 20 * np.log10(abs(g))


@pytest.mark.skipif(not os.path.exists(os.path.join(REFERENCE, "C_pitch+3.wav")),
                    reason="素材のフォルダに praat-reference/ が無い（評価の段階の書き出し）")
@pytest.mark.parametrize("sym,clip_name,st", [("C", "clip_c", 3), ("A", "clip_a", -2)])
def test_pitch_matches_reference_praat(sym, clip_name, st, request):
    """全体を ±半音: 評価の段階の parselmouth の書き出しとサンプル単位で同じ（SNR 100 dB 超、音量差 0.1 dB 以内）。"""
    from vocal_engine.audio import read_mono
    from vocal_engine.render.pipeline import Segment
    r, x, sr = _renderer(request.getfixturevalue(clip_name))
    dur = len(x) / sr
    y, _ = r.render_range(0.0, dur, [Segment(0.0, dur, cents=100.0 * st)])
    ref, _ = read_mono(os.path.join(REFERENCE, "%s_pitch%+d.wav" % (sym, st)))
    assert len(y) == len(ref)
    corr, snr, gain_db = _gain_matched(ref, y)
    assert snr > 100.0, "基準との SNR %.1f dB（corr %.6f）" % (snr, corr)
    assert abs(gain_db) < 0.1, "基準との音量差 %.2f dB" % gain_db


def _f0_err_cents(y, sr, f0_ref, voiced_ref, factor_at, t0, t1, hop=0.010):
    from vocal_engine.analysis.f0 import estimate_f0
    fo = estimate_f0(x=y, sr=sr)
    i0, i1 = int(t0 / hop), int(t1 / hop)
    i1 = min(i1, len(fo.f0), len(f0_ref))
    idx = np.arange(i0, i1)
    m = voiced_ref[idx] & fo.voiced[idx] & (f0_ref[idx] > 0) & (fo.f0[idx] > 0)
    got = 1200 * np.log2(fo.f0[idx][m] / f0_ref[idx][m])
    want = np.array([factor_at(i * hop) for i in idx[m]])
    return np.abs(got - want)


def test_pitch_shift_hits_target(clip_c):
    """C の 0.3〜1.9 s を +3 半音: 出力の F0 の中央値誤差 10 セント以内（段階0の praat は 1.6）。"""
    from vocal_engine.render.pipeline import Segment
    r, x, sr = _renderer(clip_c)
    f0r = clip_c[2]
    y, _ = r.render_range(0.0, len(x) / sr, [Segment(0.3, 1.9, cents=300.0)])
    err = _f0_err_cents(y, sr, f0r.f0, f0r.voiced, lambda t: 300.0, 0.35, 1.85)
    assert len(err) > 50
    assert np.median(err) < 10.0, "中央値誤差 %.1f セント" % np.median(err)


def test_pitch_curve_follows_points(clip_a):
    """A の 0.4〜1.6 s に 0 → +200 セントの曲線: 中央値誤差 10 セント以内。"""
    from vocal_engine.render.pipeline import Segment
    r, x, sr = _renderer(clip_a)
    f0r = clip_a[2]
    y, _ = r.render_range(0.0, len(x) / sr,
                          [Segment(0.4, 1.6, curve_points=[[0.0, 0.0], [1.2, 200.0]])])
    assert len(y) == len(x)
    err = _f0_err_cents(y, sr, f0r.f0, f0r.voiced,
                        lambda t: float(np.interp(t - 0.4, [0.0, 1.2], [0.0, 200.0])), 0.45, 1.55)
    assert len(err) > 50
    assert np.median(err) < 10.0, "中央値誤差 %.1f セント" % np.median(err)


@pytest.mark.parametrize("ratio", [0.8, 1.3])
def test_stretch_length_and_pitch(ratio):
    """D 全体を 0.8 / 1.3 倍: 長さが ratio どおり、ピッチは変わらない（中央値 15 セント以内）。"""
    from vocal_engine.analysis.f0 import estimate_f0
    from vocal_engine.render.base import get_backend
    from vocal_engine.render.pipeline import Segment
    clip = _load(CLIP_D)
    r, x, sr = _renderer(clip)
    f0r = clip[2]
    dur = len(x) / sr
    y, _ = r.render_range(0.0, dur, [Segment(0.0, dur, ratio=ratio)])
    assert len(y) == int(round(len(x) * ratio))
    # バックエンド単体でも Praat の出力をそのまま切り出して ratio どおり（_fit に頼らない）
    be = get_backend("praat")
    yb, info = be.render(r.ctx, 0.5, 1.5, ratio=ratio)
    assert info["out_samples"] == int(round(sr * ratio))
    assert len(yb) == info["out_samples"]
    fo = estimate_f0(x=y, sr=sr)
    t_out = np.arange(len(fo.f0)) * 0.010
    src = np.clip(np.round(t_out / ratio / 0.010).astype(int), 0, len(f0r.f0) - 1)
    m = fo.voiced & f0r.voiced[src] & (fo.f0 > 0) & (f0r.f0[src] > 0)
    err = np.abs(1200 * np.log2(fo.f0[m] / f0r.f0[src][m]))
    assert m.sum() > 50
    assert np.median(err) < 15.0, "中央値誤差 %.1f セント" % np.median(err)


@pytest.mark.parametrize("cents", [100.0, -200.0])
def test_seam_has_no_level_bump_or_click(clip_c, cents):
    """区間の境界 ±10 ms の音量が原音から ±1.0 dB、区間の中は ±0.5 dB、境界にクリックなし。

    音量合わせ（praat.LEVEL_MATCH）が無いと −2 半音で区間の中が −1.4 dB、境界で −0.7 dB だった。"""
    from vocal_engine.render.pipeline import Segment
    r, x, sr = _renderer(clip_c)
    dur = len(x) / sr
    y, _ = r.render_range(0.0, dur, [Segment(0.5, 1.16029, cents=cents)])
    assert len(y) == len(x)
    for b in (0.5, 1.16029):
        c, h = int(round(b * sr)), int(0.010 * sr)
        db = 10 * np.log10(np.mean(y[c - h:c + h] ** 2) / np.mean(x[c - h:c + h] ** 2))
        assert abs(db) < 1.0, "境界 %.3f s の音量差 %.2f dB" % (b, db)
    a0, b0 = int(0.52 * sr), int(1.14 * sr)
    inner = 10 * np.log10(np.mean(y[a0:b0] ** 2) / np.mean(x[a0:b0] ** 2))
    assert abs(inner) < 0.5, "区間の中の音量差 %.2f dB" % inner
    dd = np.abs(np.diff(y))
    p = np.percentile(dd, 99.9)
    for b in (0.5, 1.16029):
        c, h = int(round(b * sr)), int(0.005 * sr)
        assert dd[c - h:c + h].max() < p
    # 区間の外は原音のまま（クロスフェードの 10 ms より外）
    h = int(0.011 * sr)
    assert np.array_equal(y[:int(round(0.5 * sr)) - h], x[:int(round(0.5 * sr)) - h])
    assert np.array_equal(y[int(round(1.16029 * sr)) + h:], x[int(round(1.16029 * sr)) + h:])


def test_harmonic_noise_floor_not_worse_than_reference(clip_c):
    """C を +3 半音: 高調波間比の低下が 8 dB 以内（評価の段階の praat は原音 36.0 → 29.1 dB、−7.0 dB）。

    自前 psola（−0.6 dB）より悪いのは Praat の既知の性質。ここでは評価の段階より悪くならないことだけを見る。"""
    from test_psola_quality import _harmonic_to_interharmonic
    from vocal_engine.render.pipeline import Segment
    r, x, sr = _renderer(clip_c)
    f0r = clip_c[2]
    dur = len(x) / sr
    y, _ = r.render_range(0.0, dur, [Segment(0.0, dur, cents=300.0)])
    f0 = np.where(f0r.voiced, f0r.f0, 0.0)
    h_orig = _harmonic_to_interharmonic(x, sr, f0)
    h_out = _harmonic_to_interharmonic(y, sr, f0 * 2 ** (3 / 12))
    assert h_out > h_orig - 8.0, "高調波間比 原音 %.1f dB → +3 半音 %.1f dB" % (h_orig, h_out)


def test_connection_edits_render_with_praat(clip_c):
    """無音の挿入・切り取り・移動を含む区間列が、累積位置どおりの長さで出る（Praat 既定）。"""
    from vocal_engine.render.pipeline import Segment
    r, x, sr = _renderer(clip_c)
    dur = len(x) / sr
    segs = [Segment(0.3, 0.8, cents=100.0),
            Segment(0.8, 0.8, silence_sec=0.25),
            Segment(0.8, 1.2, ratio=1.25),
            Segment(1.4, 1.5, ratio=0.0),
            Segment(2.4, 2.9, curve_points=[[0.0, -100.0], [0.5, 100.0]])]
    y, info = r.render_range(0.0, dur, segs)
    want = int(round((dur + 0.25 + 0.4 * 0.25 - 0.1) * sr))
    assert abs(len(y) - want) <= 1
    assert info["backend"] == "praat" and not info["warnings"]
    assert np.all(np.isfinite(y)) and np.max(np.abs(y)) < 1.5
