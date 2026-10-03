# -*- coding: utf-8 -*-
"""再合成のつなぎ目に出る 1 サンプルのクリック（プチ）の回帰テスト。

合成音（倍音のある持続音。ビブラート付き）だけで確かめる（素材・モデル不要）。持続音は 2 階差分が小さく
滑らかなので、つなぎ目に出る「元に無い 1 サンプルのスパイク」と「振幅の段差」が数字にはっきり出る
（直す前は 2 階差分の最大が入力の 100 倍を超えた）。

  E1  区間の長さ（累積の秒を丸めた want）と、再合成の core の長さ（区間を個別に丸めたもの）が 1 サンプル
      ずれると、core の末尾に 0 が足されて、クロスフェードの中央の 1 サンプルが半分ほどに落ちる
  E2  出力が 20 ms 未満の区間（「ガイドへ寄せる」の細かい伸縮は 0.5 ms まである）は、クロスフェードが
      区間の長さまで縮んで、前の再合成の端と次の音が段差でつながる。20 ms 未満の隙間で頭と尻の
      クロスフェードが重なって上書きし合うのも同じ段差になる
"""
import numpy as np
import pytest

from vocal_engine.render import pipeline as P
from vocal_engine.render.pipeline import Renderer, Segment, bundle_short_segments

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


@pytest.mark.parametrize("backend", _backends())
def test_stretches_between_gaps_have_no_click(voice, backend):
    """原音のままの隙間（長さが毎回違う）をはさんだ伸縮。隙間の長さの丸めも 1 サンプルずれる。"""
    rng = np.random.default_rng(2)
    segs = _chain(rng, rng.uniform(0.02, 0.06, 30), 0.6, 1.8, gap_lo=0.0153, gap_hi=0.07)
    y, _ = _render(voice, backend, segs)
    _assert_clean(voice, y, segs)


@pytest.mark.parametrize("backend", _backends())
def test_tiny_touching_stretches_have_no_step(voice, backend):
    """0.5〜10 ms の極短い伸縮が接して並ぶ（「ガイドへ寄せる」の細かい伸縮）。"""
    rng = np.random.default_rng(3)
    durs = rng.choice([0.0005, 0.001, 0.002, 0.005, 0.008, 0.03], 60)
    segs = _chain(rng, durs, 0.5, 2.0)
    y, _ = _render(voice, backend, segs)
    _assert_clean(voice, y, segs)


@pytest.mark.parametrize("backend", _backends())
def test_tiny_isolated_stretches_have_no_step(voice, backend):
    """0.5〜8 ms の伸縮が、15〜60 ms の隙間をはさんで孤立している（隙間から借りて広げる）。"""
    rng = np.random.default_rng(5)
    durs = rng.choice([0.0005, 0.001, 0.002, 0.005, 0.008], 30)
    segs = _chain(rng, durs, 0.5, 2.5, gap_lo=0.015, gap_hi=0.06)
    y, _ = _render(voice, backend, segs)
    _assert_clean(voice, y, segs)


@pytest.mark.parametrize("backend", _backends())
def test_tiny_stretches_with_pitch_have_no_step(voice, backend):
    """ピッチの違う極短い区間と長い区間が接して並ぶ。"""
    rng = np.random.default_rng(7)
    durs = rng.choice([0.002, 0.005, 0.03, 0.05], 30)
    segs = _chain(rng, durs, 0.6, 1.6, cents=[0, 200, -300])
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


def test_bundle_merges_short_touching_into_longer_neighbour():
    min_out = 0.02
    a = Segment(1.0, 1.1, cents=200.0, ratio=1.0, edit_ids=["a"])
    b = Segment(1.1, 1.103, cents=0.0, ratio=3.0, edit_ids=["b"])      # 出力 9 ms
    c = Segment(1.103, 1.2, cents=-100.0, ratio=1.0, edit_ids=["c"])
    out = bundle_short_segments([a, b, c], 0.0, 5.0, min_out)
    assert len(out) == 2
    # b は長い方（出力 100 ms の a か 97 ms の c。a の方が長い）へ。ピッチ・id は a、伸縮比だけ変わる
    m = out[0]
    assert (m.start_sec, m.end_sec) == (1.0, 1.103) and m.cents == 200.0
    assert m.edit_ids == ["a", "b"]
    assert m.ratio == pytest.approx((0.1 + 0.009) / 0.103)
    assert out[1] is c
    # 出力の長さの合計は変わらない
    tot = lambda segs: sum((s.end_sec - s.start_sec) * s.ratio for s in segs)
    assert tot(out) == pytest.approx(tot([a, b, c]))
    # 渡した区間は書き換えない
    assert (b.start_sec, b.ratio, a.end_sec) == (1.1, 3.0, 1.1)


def test_bundle_shifts_curve_when_tiny_precedes():
    tiny = Segment(1.0, 1.002, ratio=2.0)
    big = Segment(1.002, 1.2, ratio=1.0, curve_points=[[0.0, 0.0], [0.1, 100.0]])
    (m,) = bundle_short_segments([tiny, big], 0.0, 5.0, 0.02)
    assert m.start_sec == 1.0 and m.end_sec == 1.2
    assert [t for t, _ in m.curve_points] == [pytest.approx(0.002), pytest.approx(0.102)]
    assert big.curve_points == [[0.0, 0.0], [0.1, 100.0]]


def test_bundle_keeps_barriers_and_borrows_from_gaps():
    mute = Segment(1.0, 1.1, gain=0.0)
    tiny = Segment(1.1, 1.102, ratio=2.0)                       # 出力 4 ms。左は無音にした区間に接している
    sil = Segment(1.5, 1.5, silence_sec=0.05)
    out = bundle_short_segments([mute, tiny, sil], 0.0, 5.0, 0.02)
    assert out[0] is mute and out[2] is sil                     # 境目は束ねない
    g = out[1]
    assert g.start_sec == pytest.approx(1.1)                    # 左は接しているので借りない
    assert g.end_sec > 1.102                                    # 右の隙間から借りる
    assert (g.end_sec - g.start_sec) * g.ratio == pytest.approx(0.02)
    # 出力の長さの合計は変わらない（借りたぶんは等倍で足す）
    assert (g.end_sec - g.start_sec) * g.ratio - 0.004 == pytest.approx(g.end_sec - 1.102)


@pytest.mark.parametrize("gap", [300, 200, 100])
def test_join_crossfades_do_not_overlap_in_short_chunk(gap):
    """短い chunk（20 ms 未満の隙間）の頭と尻のクロスフェードは重ならない（重なると後から書く方が段差で上書きする）。"""
    x = np.sin(2 * np.pi * 200 * np.arange(SR) / SR)
    r = Renderer(x, SR, np.full(101, 200.0), np.ones(101, dtype=bool), HOP, backend="psola")

    def ch(a, n, edited):
        # 再合成した音のつもりで符号を逆にする（原音とは相関 −1。混ぜ具合の違いが段差に出る）
        c = {"kind": "edited" if edited else "gap", "src": (a / SR, (a + n) / SR),
             "audio": (-x[a:a + n] if edited else x[a:a + n]).copy()}
        if edited:
            c["pre"], c["post"] = -x[a - HX:a], -x[a + n:a + n + HX]
        return c

    chunks = [ch(1010, 2000, True), ch(3010, gap, False), ch(3010 + gap, 2000, True)]
    used = []
    y = r._join(chunks, HX, 2 * HX, used=used)
    assert len(y) == 4000 + gap and len(used) == 2
    # 200 Hz・振幅 1 の正弦波の最大の傾きは 0.052。重なって上書きすると 0.27〜0.5 の段差が出た
    assert np.abs(np.diff(y)).max() < 0.06


def _junction_rhos(voice, backend, monkeypatch, align_ms):
    monkeypatch.setattr(P, "ALIGN_MAX_MS", align_ms)
    segs = _chain(np.random.default_rng(11), _durs(), 0.7, 1.4)
    y, info = _render(voice, backend, segs)
    return y, info, segs


def _durs():
    return np.random.default_rng(12).uniform(0.035, 0.07, 14)


@pytest.mark.parametrize("backend", _backends())
def test_phase_alignment_raises_junction_correlation(voice, backend, monkeypatch):
    """接して並ぶ再合成の区間は、つなぎ目で位相がそろう（そろえないと相関 rho が 0 に近く、等パワーで混ざって痩せる）。"""
    _, off, _ = _junction_rhos(voice, backend, monkeypatch, 0.0)
    y, on, segs = _junction_rhos(voice, backend, monkeypatch, 5.0)
    rho_off, rho_on = np.array(off["xfade_rhos"]), np.array(on["xfade_rhos"])
    assert np.median(rho_on) >= 0.9
    assert np.median(rho_on) - np.median(rho_off) >= 0.2
    tau = int(round(5.0 / 1000 * SR))
    assert len(on["xfade_lags"]) == on["chunks"] - 1
    assert max(abs(v) for v in on["xfade_lags"]) <= tau and any(on["xfade_lags"])
    assert set(off["xfade_lags"]) == {0}
    _assert_clean(voice, y, segs)
    # 6 ms（持続音の 1 周期ぶん）の RMS が痩せていない
    w = int(0.006 * SR)
    c = np.cumsum(np.concatenate([[0.0], y * y]))
    rms = np.sqrt((c[w:] - c[:-w]) / w)
    assert rms.min() > 0.6 * np.median(rms)


@pytest.mark.parametrize("backend", _backends())
def test_alignment_lags_can_be_shared_across_channels(voice, backend):
    """モノラルで決めた rho・lags を渡すと、別のレンダラ（別のチャンネル）でも同じ動かし方・混ぜ方になる。"""
    x, f0, v = voice
    segs = _chain(np.random.default_rng(13), _durs(), 0.7, 1.4)
    r = Renderer(x, SR, f0, v, HOP, backend=backend)
    y1, i1 = r.render_range(T0, T1, segs)
    r2 = Renderer(x, SR, f0, v, HOP, backend=backend)
    y2, i2 = r2.render_range(T0, T1, segs, xfade_rhos=i1["xfade_rhos"], xfade_lags=i1["xfade_lags"])
    np.testing.assert_allclose(y2, y1)
    assert i2["xfade_lags"] == i1["xfade_lags"] and any(i1["xfade_lags"])


@pytest.mark.parametrize("backend", _backends())
def test_alignment_never_moves_unedited_samples(voice, backend):
    """原音のままの区間は動かさない（位相をそろえるのは再合成した区間だけ）。範囲外はサンプル一致。"""
    x = voice[0]
    edits = [(1.0, 1.06, 150.0), (1.5, 1.52, -200.0)]
    segs = [Segment(a, b, cents=c) for a, b, c in edits]
    y, _ = _render(voice, backend, segs)
    assert len(y) == int(round((T1 - T0) * SR))
    ref = x[int(round(T0 * SR)):int(round(T1 * SR))]
    pad = 2 * HX + int(0.006 * SR)             # クロスフェード（片側 10 ms）と、区間を動かす最大（5 ms）の外
    mask = np.ones(len(y), dtype=bool)
    for a, b, _ in edits:
        mask[int(round((a - T0) * SR)) - pad:int(round((b - T0) * SR)) + pad] = False
    assert mask.sum() > 0.9 * len(y)
    np.testing.assert_array_equal(y[mask], ref[mask])


def test_align_finds_known_lag():
    """再合成した区間が既知のサンプル数だけ位相のずれた正弦波なら、そのずれを返す。"""
    z = lambda m: np.sin(2 * np.pi * 300.0 * np.asarray(m) / SR)         # 周期 80 サンプル
    n, npre, ahead = 1000, 600, 20
    y = z(np.arange(2000) - npre + 5000 + ahead)                        # core の先頭が 20 サンプル先の位相
    R = {"full": (y, npre), "audio": np.zeros(n)}
    l_audio, l_post = z(np.arange(5000 - 2400, 5000)), z(np.arange(5000, 5000 + HX))
    r = Renderer(z(np.arange(SR)), SR, np.full(101, 300.0), np.ones(101, dtype=bool), HOP,
                 backend="psola")
    d = r._align(l_audio, l_post, R, HX, HX, 60, None)
    assert abs(d - ahead) <= 1               # 正弦波は相関が平らなので 1 サンプルは許す
    assert r._align(l_audio, l_post, R, HX, HX, 60, 7) == 7              # 渡された量はそのまま
    assert r._align(l_audio, l_post, R, HX, HX, 60, 10_000) <= 60        # 動かせる範囲に丸める
