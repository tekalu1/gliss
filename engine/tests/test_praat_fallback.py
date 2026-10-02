# -*- coding: utf-8 -*-
"""Praat バックエンドが出せない区間の扱い（2026-09-23 のレビュー指摘の回帰テスト）。

合成音（150 Hz のパルス列っぽい音）だけで確かめる（素材・モデル不要）:
  1. 伸縮の比が 3 を超えると Praat は 3 倍で頭打ち → 自前 psola に落として長さと中身を守る（3.5 / 4 / 8）。
     小さい側（0.05）は Praat のまま
  2. 狙う F0 が約 50 Hz を下回ると Praat はピッチを変えない → psola に落として warnings に出す
  3. ごく短い素材で Praat の解析が例外 → psola に落とす
  4. Praat の出力長が期待値から大きくずれたら psola に落とす（保険）
  5. ステレオ: モノラル化した音の解析とゲインを全チャンネルで共有する（書き出し全体は test_export.py）
  6. MCP の `_renderer` キャッシュのキーは実際に使うバックエンド名
"""
import numpy as np
import pytest

praat = pytest.importorskip("vocal_engine.render.praat")
pytestmark = pytest.mark.skipif(not praat.available(), reason="praat-parselmouth が入っていない")

SR = 22050
F0 = 150.0


def _voice(dur=1.0, amp=1.0):
    t = np.arange(int(SR * dur)) / SR
    x = 0.15 * np.sign(np.sin(2 * np.pi * F0 * t)) + 0.1 * np.sin(2 * np.pi * 2 * F0 * t)
    return amp * x


def _f0_track(dur=1.0, f0=F0):
    n = int(dur / 0.01) + 1
    return np.full(n, f0), np.ones(n, dtype=bool)


def _ctx(x, **kw):
    f0, v = _f0_track(len(x) / SR)
    return praat.PraatContext(x, SR, f0, v, **kw)


def _median_f0(y):
    import parselmouth
    f = parselmouth.Sound(y, SR).to_pitch(pitch_floor=20.0).selected_array["frequency"]
    return float(np.median(f[f > 0]))


def _rms(y):
    return float(np.sqrt(np.mean(np.asarray(y) ** 2)))


@pytest.fixture(scope="module")
def ctx():
    return _ctx(_voice())


@pytest.mark.parametrize("ratio", [3.5, 4.0, 8.0])
def test_stretch_above_praat_limit_falls_back_to_psola(ctx, ratio):
    y, info = praat.render_segment(ctx, 0.3, 0.5, ratio=ratio)
    want = int(round(0.2 * SR * ratio))
    assert len(y) == want
    assert info["backend"] == "psola" and info["requested_backend"] == "praat"
    assert "上限" in info["fallback_reason"] and info["warnings"]
    # 末尾 100 ms まで音が入っている（Praat のままだと 3 倍を過ぎた先が 0 だった）
    tail = y[-int(0.1 * SR):]
    assert _rms(tail) > 0.3 * _rms(y)


def test_stretch_at_and_below_limit_stays_praat(ctx):
    for ratio in (0.05, 3.0):
        y, info = praat.render_segment(ctx, 0.3, 0.5, ratio=ratio)
        assert len(y) == int(round(0.2 * SR * ratio))
        assert info["backend"] == "praat", info
        assert _rms(y[-max(1, len(y) // 10):]) > 0.01


def test_low_target_f0_falls_back_to_psola(ctx):
    y, info = praat.render_segment(ctx, 0.2, 0.8, cents=-2400)       # 150 Hz → 37.5 Hz
    assert info["backend"] == "psola"
    assert "F0" in info["fallback_reason"]
    assert abs(1200 * np.log2(_median_f0(y) / 37.5)) < 50
    # 下限より上なら Praat のまま効く
    y, info = praat.render_segment(ctx, 0.2, 0.8, cents=-1200)       # → 75 Hz
    assert info["backend"] == "praat"
    assert abs(1200 * np.log2(_median_f0(y) / 75.0)) < 50


def test_low_target_f0_in_curve_falls_back(ctx):
    _, info = praat.render_segment(ctx, 0.2, 0.8, curve_points=[(0.0, 0.0), (0.6, -2400.0)])
    assert info["backend"] == "psola"


def test_short_source_falls_back_instead_of_raising():
    x = _voice(0.05)
    n = 6
    c = praat.PraatContext(x, SR, np.full(n, 60.0), np.ones(n, dtype=bool))
    assert c.error                                     # "minimum pitch must not be less than ..."
    y, info = praat.render_segment(c, 0.01, 0.04, cents=100)
    assert len(y) == int(round(0.03 * SR))
    assert info["backend"] == "psola" and "解析" in info["fallback_reason"]


def test_length_mismatch_guard(ctx, monkeypatch):
    # 上限の判定を外して Praat に 4 倍を頼む → 3 倍しか出ないので長さの保険で psola に落ちる
    monkeypatch.setattr(praat, "PRAAT_MAX_RATIO", 10.0)
    y, info = praat.render_segment(ctx, 0.3, 0.5, ratio=4.0)
    assert info["backend"] == "psola"
    assert "期待" in info["fallback_reason"]
    assert len(y) == int(round(0.2 * SR * 4.0))


def test_renderer_reports_fallback_in_warnings():
    from vocal_engine.render.pipeline import Renderer, Segment
    x = _voice()
    f0, v = _f0_track()
    r = Renderer(x, SR, f0, v, 0.01, backend="praat")
    y, info = r.render_range(0.0, 1.0, [Segment(0.3, 0.4, ratio=4.0)])
    assert info["backend"] == "praat"
    assert any("psola" in w and "上限" in w for w in info["warnings"])
    assert len(y) == int(round((0.9 + 0.1 * 4.0) * SR))


def test_stereo_shares_analysis_and_gain():
    from vocal_engine.render.pipeline import Renderer
    left = _voice()
    right = 0.4 * _voice() + 0.02 * np.sin(2 * np.pi * 700 * np.arange(len(left)) / SR)
    f0, v = _f0_track()
    mono = Renderer(0.5 * (left + right), SR, f0, v, 0.01, backend="praat")
    rl = Renderer(left, SR, f0, v, 0.01, backend="praat", ref=mono)
    rr = Renderer(right, SR, f0, v, 0.01, backend="praat", ref=mono)
    assert rl.ctx.pulses is mono.ctx.pulses and rr.ctx.pulses is mono.ctx.pulses
    yl, il = praat.render_segment(rl.ctx, 0.3, 0.6, cents=300)
    yr, ir = praat.render_segment(rr.ctx, 0.3, 0.6, cents=300)
    assert il["shared_level"] and ir["shared_level"]
    assert il["level_match_db"] == ir["level_match_db"]
    # 同じ倍率の信号なら出力も同じ倍率（ゲインがチャンネルで揃っている）
    yl2, _ = praat.render_segment(_ctx(left, ref=mono.ctx), 0.3, 0.6, cents=300)
    np.testing.assert_allclose(yl2, yl)


def test_mcp_renderer_cache_key_uses_resolved_backend(monkeypatch):
    from vocal_engine import mcp_server as m

    class P:
        take_f0 = type("F", (), {"f0": _f0_track()[0], "voiced": _f0_track()[1], "hop_s": 0.01})()

        def audio(self, kind):
            return _voice(), SR

    built = []

    class FakeRenderer:
        def __init__(self, *a, backend=None, **kw):
            built.append(backend)

    monkeypatch.setattr(m, "_project", lambda required=True: P())
    monkeypatch.setattr(m, "Renderer", FakeRenderer)
    monkeypatch.setattr(m, "resolve_backend_name", lambda b=None: "psola")   # praat が無い環境のふり
    monkeypatch.setitem(m._state, "renderer", None)
    monkeypatch.setitem(m._state, "renderer_backend", None)
    m._renderer("praat")
    m._renderer("psola")
    m._renderer("praat")
    assert built == ["psola"]
