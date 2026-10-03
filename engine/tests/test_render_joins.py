# -*- coding: utf-8 -*-
"""再合成のつなぎ目に出る 1 サンプルのクリック（プチ）の回帰テスト。

合成音（倍音のある持続音。ビブラート付き）だけで確かめる（素材・モデル不要）。持続音は 2 階差分が小さく
滑らかなので、つなぎ目に出る「元に無い 1 サンプルのスパイク」と「振幅の段差」が数字にはっきり出る
（直す前は 2 階差分の最大が入力の 100 倍を超えた）。

  E1  区間の長さ（累積の秒を丸めた want）と、再合成の core の長さ（区間を個別に丸めたもの）が 1 サンプル
      ずれると、core の末尾に 0 が足されて、クロスフェードの中央の 1 サンプルが半分ほどに落ちる
"""
import numpy as np
import pytest

from vocal_engine.render import pipeline as P
from vocal_engine.render.pipeline import Renderer, Segment

SR = 24000
HOP = 0.01
T0, T1 = 0.2, 2.8
HX = int(round(P.XFADE_MS / 2000.0 * SR))          # 片側のクロスフェード = 10 ms


def _backends():
    out = ["psola"]
    try:
        from vocal_engine.render import praat
        if praat.available():
            out.insert(0, "praat")
    except Exception:                               # pragma: no cover
        pass
    return out


def _voice(dur=3.0):
    """170 Hz 前後（ビブラート ±2.5%）の 6 倍音の持続音。"""
    t = np.arange(int(SR * dur)) / SR
    f = 170.0 * (1 + 0.025 * np.sin(2 * np.pi * 5.5 * t))
    ph = 2 * np.pi * np.cumsum(f) / SR
    x = sum(np.sin(k * ph) / (k * k) for k in range(1, 7))
    x = 0.4 * x / np.max(np.abs(x))
    idx = np.clip((np.arange(0, dur, HOP) * SR).astype(int), 0, len(f) - 1)
    return x, f[idx], np.ones(len(idx), dtype=bool)


@pytest.fixture(scope="module")
def voice():
    return _voice()


def _render(voice, backend, segs):
    x, f0, v = voice
    r = Renderer(x, SR, f0, v, HOP, backend=backend)
    y, info = r.render_range(T0, T1, segs)
    return y, info


def _d1(y):
    return np.abs(np.diff(y))


def _d2(y):
    return np.abs(y[1:-1] - (y[:-2] + y[2:]) / 2.0)


def _expect_len(segs):
    """出力の長さ = 累積の秒を丸めた差（`slot()` の約束。束ねても借りても変わらない）。"""
    end = T1 + sum((s.ratio - 1.0) * (s.end_sec - s.start_sec) for s in segs)
    return int(round(end * SR)) - int(round(T0 * SR))


def _assert_clean(voice, y, segs):
    """スパイク（2 階差分）も段差（1 階差分）も、入力の持続音の範囲に収まっている。

    （つなぎ目の位相のずれ＝ E3 で振幅が局所的に痩せるのは、ここでは見ない）"""
    x = voice[0][int(T0 * SR):int(T1 * SR)]
    assert len(y) == _expect_len(segs)
    d2, d1 = _d2(y), _d1(y)
    bad2 = np.flatnonzero(d2 > 3.0 * _d2(x).max())
    assert len(bad2) == 0, "元に無いスパイク %d 個（最大 %.4f。入力は %.4f）。出力の秒 %s" % (
        len(bad2), d2.max(), _d2(x).max(), np.round(T0 + (bad2[:5] + 1) / SR, 4))
    assert d1.max() < 1.5 * _d1(x).max(), "段差 %.4f（入力の最大の傾き %.4f）" % (d1.max(), _d1(x).max())


def _chain(rng, durs, ratio_lo, ratio_hi, gap_lo=0.0, gap_hi=0.0, t=0.3, cents=None):
    segs = []
    for i in range(len(durs)):
        d = float(durs[i])
        sg = Segment(t, t + d, ratio=float(rng.uniform(ratio_lo, ratio_hi)))
        if cents is not None:
            sg.cents = float(rng.choice(cents))
        segs.append(sg)
        t += d + (float(rng.uniform(gap_lo, gap_hi)) if gap_hi > 0 else 0.0)
    return segs


@pytest.mark.parametrize("backend", _backends())
def test_stretch_chain_with_rounding_mismatch_has_no_click(voice, backend):
    """区間ごとの丸めと累積の丸めがずれる伸縮の並び（接している 40 個）。"""
    rng = np.random.default_rng(1)
    segs = _chain(rng, [0.0371 + 0.0003 * i for i in range(40)], 0.7, 1.4)
    # 前提: want（累積）と core（区間ごと）が 1 サンプルずれる区間が、ちゃんと何個もある
    pos, mism = 0.0, 0
    for sg in segs:
        a = int(round(pos * SR))
        pos += (sg.end_sec - sg.start_sec) * sg.ratio
        want = int(round(pos * SR)) - a
        own = int(round(sg.end_sec * SR)) - int(round(sg.start_sec * SR))
        mism += want != int(round(own * sg.ratio))
    assert mism >= 5
    y, _ = _render(voice, backend, segs)
    _assert_clean(voice, y, segs)


def test_fit_pads_with_last_sample_not_zero():
    """足りない 1〜2 サンプルは 0 でなく最後のサンプルで埋める（0 はクロスフェードの中央でクリックになる）。"""
    y = np.array([0.1, 0.2, -0.3])
    out = P._fit(y, 5)
    np.testing.assert_array_equal(out, [0.1, 0.2, -0.3, -0.3, -0.3])
    np.testing.assert_array_equal(P._fit(y, 2), [0.1, 0.2])
    far = P._fit(y, 10)                        # 大きな不足（バックエンドの異常）は 0 のまま
    assert far[3:].tolist() == [0.0] * 7
    assert len(P._fit(np.zeros(0), 3)) == 3
