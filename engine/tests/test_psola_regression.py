# -*- coding: utf-8 -*-
"""自前 TD-PSOLA の回帰値。

`regression_measure.py` が素材のフォルダに書いた `regression-baseline.json` と突き合わせる。
品質が落ちたら気づけるようにするのが目的。
"""
import json
import os

import numpy as np
import pytest

import regression_measure as RM
from conftest import CLIP_E, needs_clips, needs_model

TOL_CENTS = 12.0          # 回帰値からのずれの許容（セント）
TOL_MCD = 0.4             # 同（dB）

pytestmark = [needs_clips, needs_model]


@pytest.fixture(scope="module")
def baseline():
    if not os.path.exists(RM.baseline_path()):
        pytest.skip("素材のフォルダに regression-baseline.json が無い（regression_measure.py を先に走らせる）")
    with open(RM.baseline_path(), encoding="utf-8") as f:
        return json.load(f)["psola"]


def _render_and_measure(sym, kind, params):
    out, ref, name, info, r = RM.render_case(sym, kind, params)
    return RM.measure(ref, out, kind, params)


@pytest.mark.parametrize("sym,kind,params,edit", [
    ("A", "pitch", {"semitones": 3}, "A_pitch+3"),
    ("C", "pitch", {"semitones": 3}, "C_pitch+3"),
    ("E", "pitch", {"semitones": 3}, "E_pitch+3"),
    ("A", "stretch", {"ratio": 1.3, "voiced_only": False}, "A_stretch1.3"),
    ("C", "stretch", {"ratio": 1.3, "voiced_only": False}, "C_stretch1.3"),
    ("E", "stretch", {"ratio": 1.3, "voiced_only": False}, "E_stretch1.3"),
])
def test_regression_values(baseline, sym, kind, params, edit):
    base = {r["edit"]: r for r in baseline}[edit]
    m = _render_and_measure(sym, kind, params)
    rmse = m["f0"]["rmse_cents"]
    mcd = m["mcd_db"]
    assert rmse <= base["f0_rmse_cents"] + TOL_CENTS, \
        "%s の F0 RMSE が悪化: %.1f → %.1f c" % (edit, base["f0_rmse_cents"], rmse)
    assert mcd <= base["mcd_db"] + TOL_MCD, \
        "%s の MCD が悪化: %.2f → %.2f dB" % (edit, base["mcd_db"], mcd)
    # 中央値は「ふだんどれだけ正確か」。外れフレームに引っ張られない指標として固定する。
    assert m["f0"]["median_abs_cents"] <= base["f0_median_abs_cents"] + 4.0


def test_E_stretch_beats_parselmouth_psola(baseline):
    """評価の段階の parselmouth 版（180〜283 セント）より明確に良いこと。"""
    base = {r["edit"]: r for r in baseline}["E_stretch1.3"]
    assert base["f0_rmse_cents"] < 100.0
    assert base["f0_median_abs_cents"] < 15.0
    assert base["f0_over50c_pct"] < 10.0


def test_E_stretch_excluding_worst_frame_under_50c():
    """外れ 1 フレーム（音節境界の推定ゆれ）を除けば 50 セントを切ること。

    E の 0.829 s は RMVPE が原音の F0 を 652 → 351 Hz と 1 オクターブ落とす音節の境界で、
    64 ms の解析窓に両方の音節が半々に入る。原音側と出力側でどちらに倒れるかが
    分かれるため、そのフレームだけ 1200 セントの誤差になる（合成の問題ではない）。
    """
    out, ref, name, info, r = RM.render_case("E", "stretch", {"ratio": 1.3})
    f0_ref, _ = RM.f0_of(ref)
    f0_out, _ = RM.f0_of(out)
    idx = np.clip(np.round(np.arange(len(f0_out)) / 1.3).astype(int), 0, len(f0_ref) - 1)
    tgt = f0_ref[idx]
    v = (f0_out > 0) & (tgt > 0)
    d = 1200.0 * np.log2(f0_out[v] / tgt[v])
    worst = np.argsort(np.abs(d))[-1]
    kept = np.delete(d, worst)
    assert np.sqrt(np.mean(kept ** 2)) < 50.0


def test_identity_render_reproduces_original():
    """無編集なら原音がほぼ完全に復元されること（非対称 Hann の COLA）。"""
    from vocal_engine.analysis.f0 import estimate_f0
    from vocal_engine.audio import read_mono
    from vocal_engine.render.psola import PsolaAnalysis, synthesize
    x, sr = read_mono(CLIP_E)
    f0r = estimate_f0(x=x, sr=sr)
    ana = PsolaAnalysis(x, sr, f0r.f0, f0r.voiced, f0r.hop_s)
    y, info = synthesize(ana, 0, len(x), ratio=1.0, pitch_factor=1.0)
    n = min(len(x), len(y))
    rel = np.sqrt(np.mean((y[:n] - x[:n]) ** 2)) / np.sqrt(np.mean(x[:n] ** 2))
    assert rel < 0.05, "恒等変換で波形が変わりすぎ: 相対誤差 %.3f" % rel
    assert info["voiced_grains"] > 0
    st = ana.stats()
    assert st["n_marks"] > 100
    assert 0.5 < st["median_period_ms"] < 20


def test_pitch_marks_follow_f0_at_syllable_boundary():
    """E の音節の境界（0.83 s。F0 が約 1 オクターブ下がる）でマーク間隔が正しく切り替わること。"""
    from vocal_engine.analysis.f0 import estimate_f0
    from vocal_engine.audio import read_mono
    from vocal_engine.render.psola import PsolaAnalysis
    x, sr = read_mono(CLIP_E)
    f0r = estimate_f0(x=x, sr=sr)
    ana = PsolaAnalysis(x, sr, f0r.f0, f0r.voiced, f0r.hop_s)
    m = ana.marks
    before = m[(m > 0.80 * sr) & (m < 0.825 * sr)]
    after = m[(m > 0.833 * sr) & (m < 0.86 * sr)]
    f_before = sr / np.median(np.diff(before))
    f_after = sr / np.median(np.diff(after))
    assert 600 < f_before < 720, "境界の手前が 676 Hz 付近でない: %.0f" % f_before
    assert 320 < f_after < 400, "境界の後が 350 Hz 付近でない: %.0f" % f_after
