# -*- coding: utf-8 -*-
"""自前 TD-PSOLA の音質（段階3: 「ノイズが多い」「ロボット声」の修正）の回帰テスト。

無編集の完全復元（test_psola_regression.py）では見えない次の 3 点を固定する。
  1. ピッチマークが周期ごとのエネルギーの山（声門パルス）に乗っていること
     （旧版は基本波の山で打っていて、パルスから約半周期ずれていた）
  2. ピッチを変えたときに高調波の間にノイズ床が立たないこと
     （旧版は粒を整数サンプルに丸めて置いていて、+3 半音で 6〜8 dB 悪化していた）
  3. 編集区間と原音のつなぎ目が膨らまず、段差が出ないこと
詳細と数値は stage3-render/REPORT.md。
"""
import numpy as np
import pytest

from conftest import CLIP_A, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


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


def _mark_offsets(ana):
    """各マークから ±½ 周期の中のエネルギー最大点までのずれ（周期に対する比）。"""
    from scipy.signal import butter, sosfiltfilt
    x, sr = ana.x, ana.sr
    xh = sosfiltfilt(butter(4, 300 / (sr / 2), btype="high", output="sos"), x)
    k = int(0.0005 * sr)
    env = np.convolve(xh ** 2, np.hanning(2 * k + 1), mode="same")
    offs = []
    for i, m in enumerate(ana.marks):
        P = 0.5 * (ana.pl[i] + ana.pr[i])
        a, b = int(m - P / 2), int(m + P / 2)
        if a < 0 or b > len(x):
            continue
        offs.append((a + int(np.argmax(env[a:b])) - m) / P)
    return np.array(offs)


@pytest.mark.parametrize("which", ["A", "C"])
def test_marks_sit_on_energy_peaks(which, clip_a, clip_c):
    from vocal_engine.render.psola import PsolaAnalysis
    x, sr, f0r = clip_a if which == "A" else clip_c
    ana = PsolaAnalysis(x, sr, f0r.f0, f0r.voiced, f0r.hop_s)
    offs = _mark_offsets(ana)
    circ = np.angle(np.mean(np.exp(2j * np.pi * offs))) / (2 * np.pi)
    # 旧版: circular mean 0.46〜0.47 周期、|ずれ| > ¼ 周期が 91 %
    assert abs(circ) < 0.05, "マークがパルスから平均 %.2f 周期ずれている" % circ
    assert np.mean(np.abs(offs) > 0.25) < 0.25


def _harmonic_to_interharmonic(y, sr, f0_frames, hop_s=0.010):
    """有声で F0 が安定したフレームの、k·f0 のピークと (k+½)·f0 の谷の比（dB、〜5 kHz の平均）。"""
    n_fft, L = 4096, 2048
    w = np.hanning(L)
    df = sr / n_fft
    vals = []
    for i in range(2, len(f0_frames) - 2):
        f = f0_frames[i]
        seg = f0_frames[i - 2:i + 3]
        if f <= 0 or np.any(seg <= 0) or np.max(np.abs(1200 * np.log2(seg / f))) > 30:
            continue
        a = int(round(i * hop_s * sr)) - L // 2
        if a < 0 or a + L > len(y):
            continue
        P = np.abs(np.fft.rfft(y[a:a + L] * w, n_fft)) ** 2 + 1e-20
        r = []
        for k in range(1, int(5000 / f)):
            h, bw = int(round(k * f / df)), max(1, int(round(0.15 * f / df)))
            v, vw = int(round((k + 0.5) * f / df)), max(1, int(round(0.1 * f / df)))
            r.append(10 * np.log10(P[h - bw:h + bw + 1].max() / P[v - vw:v + vw + 1].mean()))
        if r:
            vals.append(np.mean(r))
    return float(np.mean(vals))


def test_pitch_shift_keeps_harmonic_noise_floor(clip_c):
    """C を +3 半音: 高調波間比が原音から 3 dB 以内（旧版は 7.9 dB 悪化、praat は 7.0 dB 悪化）。"""
    from vocal_engine.render.psola import PsolaAnalysis, synthesize
    x, sr, f0r = clip_c
    ana = PsolaAnalysis(x, sr, f0r.f0, f0r.voiced, f0r.hop_s)
    a = 2 ** (3 / 12)
    y, _ = synthesize(ana, 0, len(x), pitch_factor=a)
    f0 = np.where(f0r.voiced, f0r.f0, 0.0)
    h_orig = _harmonic_to_interharmonic(x, sr, f0)
    h_out = _harmonic_to_interharmonic(y, sr, f0 * a)
    assert h_out > h_orig - 3.0, "高調波間比 原音 %.1f dB → +3 半音 %.1f dB" % (h_orig, h_out)


def _renderer(clip):
    from vocal_engine.render.pipeline import Renderer
    x, sr, f0r = clip
    return Renderer(x, sr, f0r.f0, f0r.voiced, f0r.hop_s, backend="psola"), x, sr


@pytest.mark.parametrize("align_ms", [0.0, 5.0])
def test_seam_of_near_identity_edit_is_transparent(clip_c, monkeypatch, align_ms):
    """ほぼ無編集（+0.001 セント）の区間を差し込んでも、境界 ±10 ms は原音と同じ。

    旧版は原音との相対誤差 0.77〜0.87（境界で原音の継ぎ足し＋等パワーの重ね）。
    位相をそろえない（align_ms = 0）なら両端の境界が透明。そろえるとき（既定）は、頭の境界は原音に
    そろえてほぼ透明になる。尻の境界は保証しない（再合成した区間の位相は無声をまたぐなどで区間の中でずれていくので、
    頭と尻が同時にそろうとは限らない。そろわないときは従来どおり等パワーで混ざる）。"""
    from vocal_engine.render import pipeline as P
    from vocal_engine.render.pipeline import Segment
    monkeypatch.setattr(P, "ALIGN_MAX_MS", align_ms)
    r, x, sr = _renderer(clip_c)
    dur = len(x) / sr
    y, _ = r.render_range(0.0, dur, [Segment(0.5, 1.16029, cents=1e-3)])
    for b in ((0.5, 1.16029) if align_ms == 0.0 else (0.5,)):
        c, h = int(round(b * sr)), int(0.010 * sr)
        err = np.sqrt(np.mean((y[c - h:c + h] - x[c - h:c + h]) ** 2) / np.mean(x[c - h:c + h] ** 2))
        assert err < (0.3 if align_ms else 0.02), "境界 %.3f s の相対誤差 %.3f" % (b, err)


def test_seam_of_pitch_edit_has_no_level_bump(clip_c):
    """+1 半音の区間の境界 ±10 ms で、音量が原音から ±1.5 dB 以内（膨らまない・痩せない）。"""
    from vocal_engine.render.pipeline import Segment
    r, x, sr = _renderer(clip_c)
    dur = len(x) / sr
    y, _ = r.render_range(0.0, dur, [Segment(0.5, 1.16029, cents=100.0)])
    for b in (0.5, 1.16029):
        c, h = int(round(b * sr)), int(0.010 * sr)
        db = 10 * np.log10(np.mean(y[c - h:c + h] ** 2) / np.mean(x[c - h:c + h] ** 2))
        assert abs(db) < 1.5, "境界 %.3f s の音量差 %.2f dB" % (b, db)
    # 1 次差分のピーク（クリック）が曲全体の 99.9 パーセンタイルを超えない
    dd = np.abs(np.diff(y))
    p = np.percentile(dd, 99.9)
    for b in (0.5, 1.16029):
        c, h = int(round(b * sr)), int(0.005 * sr)
        assert dd[c - h:c + h].max() < p


def test_move_only_segment_is_verbatim(clip_c):
    """移動だけの区間は再合成せず原音をそのまま置く（境界のクロスフェードの外は完全一致）。"""
    from vocal_engine.render.pipeline import Segment
    r, x, sr = _renderer(clip_c)
    dur = len(x) / sr
    y, info = r.render_range(0.0, dur, [Segment(0.8, 1.4, move_ms=-30.0)])
    assert len(y) == len(x)
    d = int(round(0.030 * sr))
    a, b = int(round(0.82 * sr)), int(round(1.38 * sr))
    assert np.array_equal(y[a - d:b - d], x[a:b])
