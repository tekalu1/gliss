# -*- coding: utf-8 -*-
"""ピッチ（F0）検出の方式（RMVPE・Gliss の F0 モデル（試作）・Praat）。合成音だけで確かめる（素材は要らない）。

- どの方式も 10 ms の格子で F0 を返し、既知の音高に近い。無音は無声
- 方式の選び方（`resolve_estimator`。RMVPE の重みが無ければ Gliss のモデル）
- 解析のキャッシュが方式で分かれる（方式を替えたら解析し直し、同じ方式なら読むだけ）
"""
import os

import numpy as np
import pytest
import soundfile as sf

from vocal_engine.analysis import f0 as F

from conftest import needs_model

SR = 44100


def _tone(sec=2.0, sr=SR, semis=(0, 5), base=220.0):
    """前半 base Hz・後半 base×2^(5/12) Hz の倍音のある音。最初と最後の 0.2 秒は無音。"""
    t = np.arange(int(sr * sec)) / sr
    f = base * 2 ** (np.where(t < sec / 2, semis[0], semis[1]) / 12)
    ph = 2 * np.pi * np.cumsum(f) / sr
    x = sum(0.3 / k * np.sin(k * ph) for k in range(1, 8))
    x[: int(0.2 * sr)] = 0.0
    x[int((sec - 0.2) * sr):] = 0.0
    return x


@pytest.fixture(autouse=True)
def _reset_preferred(monkeypatch):
    monkeypatch.setattr(F, "_preferred", None)
    monkeypatch.delenv(F.ESTIMATOR_ENV, raising=False)


def _cents(a, b):
    return abs(1200.0 * np.log2(a / b))


@pytest.mark.parametrize("estimator", ["gliss", "praat", pytest.param("rmvpe", marks=needs_model)])
def test_estimators_find_known_pitch_on_10ms_grid(estimator):
    x = _tone()
    r = F.estimate_f0(x=x, sr=SR, estimator=estimator)
    n = int(np.floor(len(x) / SR / 0.01)) + 1
    assert r.estimator == estimator
    assert r.hop_s == 0.01 and r.n_frames == n
    assert len(r.confidence) == len(r.voiced) == len(r.rms_db) == n
    # 音の中ほど（境目・端の 0.1 秒を除く）
    lo, hi = r.f0[30:90], r.f0[110:170]
    assert (lo > 0).mean() > 0.95 and (hi > 0).mean() > 0.95
    assert _cents(np.median(lo), 220.0) < 20
    assert _cents(np.median(hi), 220.0 * 2 ** (5 / 12)) < 20
    # 無音（最初と最後の 0.2 秒）は無声。無声の所は F0 0
    assert not r.voiced[:15].any() and not r.voiced[-15:].any()
    assert np.all(r.f0[~r.voiced] == 0)
    assert np.all((r.confidence >= 0) & (r.confidence <= 1))
    # 保存の形（JSON）を通っても同じ方式・版
    back = F.F0Result.from_json(r.to_json())
    assert back.estimator == estimator and back.meta.get("version") == F.estimator_version(estimator)


@pytest.mark.parametrize("estimator", ["gliss", "praat"])
def test_estimators_on_short_and_silent_input(estimator):
    r = F.estimate_f0(x=np.zeros(SR), sr=SR, estimator=estimator)
    assert r.n_frames == 101 and not r.voiced.any()
    r = F.estimate_f0(x=_tone(sec=0.05)[:], sr=SR, estimator=estimator)   # 窓 1 つより短い
    assert r.n_frames == 6


def test_gliss_long_input_matches_chunked_frames():
    """長い音（1875 フレーム = 30 秒を超える）を区切って流しても、F0 が続く（区切りで外れない）。"""
    sr = 16000
    x = _tone(sec=40.0, sr=sr, semis=(0, 0))
    r = F.estimate_f0(x=x, sr=sr, estimator="gliss")
    mid = r.f0[100:3900]
    assert (mid > 0).mean() > 0.99
    assert np.max([_cents(v, 220.0) for v in mid[mid > 0]]) < 30


def test_resolve_estimator(monkeypatch):
    monkeypatch.setattr(F, "RMVPE_PATH", os.path.join(os.path.dirname(__file__), "no-such-rmvpe.onnx"))
    assert F.preferred_estimator() == "rmvpe"
    assert F.resolve_estimator() == "gliss"             # RMVPE の重みが無い: 同梱のモデルで解析する
    assert F.resolve_estimator("rmvpe") == "rmvpe"      # 名前を指定したときはそのまま
    with pytest.raises(F.ModelMissingError):
        F.estimate_f0(x=_tone(sec=0.5), sr=SR, estimator="rmvpe")
    assert F.set_preferred_estimator("praat") == "praat"
    assert F.resolve_estimator() == "praat"
    with pytest.raises(ValueError):
        F.set_preferred_estimator("nope")
    with pytest.raises(ValueError):
        F.resolve_estimator("nope")
    F.set_preferred_estimator(None)
    monkeypatch.setenv(F.ESTIMATOR_ENV, "praat")
    assert F.resolve_estimator() == "praat"
    assert F.same_estimator("gliss", "proto1", "gliss")
    assert not F.same_estimator("gliss", None, "gliss")          # 版が違う（前の試作）: 作り直す
    assert not F.same_estimator("rmvpe", None, "praat")
    assert F.same_estimator("gliss", "proto1", "auto") and F.same_estimator("rmvpe", None, "auto")


def test_bundled_model_exists():
    assert os.path.exists(F.GLISS_F0_PATH)
    assert os.path.getsize(F.GLISS_F0_PATH) < 1 << 20


def test_take_cache_is_split_by_estimator(tmp_path, monkeypatch):
    """方式を替えたらテイクとガイドを解析し直す。同じ方式に戻したガイドは鍵付きの保存を読むだけ。"""
    from vocal_engine.project import Project, store
    calls = []
    real = store.estimate_f0

    def counting(x, sr, estimator=None, sweep=False):
        calls.append(estimator)
        return real(x=x, sr=sr, estimator=estimator, sweep=sweep)

    monkeypatch.setattr(store, "estimate_f0", counting)
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")     # 歌詞の推定（音素の重み）に左右されない
    take, guide = str(tmp_path / "take.wav"), str(tmp_path / "guide.wav")
    sf.write(take, _tone(), SR)
    sf.write(guide, _tone(base=233.08), SR)
    directory = str(tmp_path / "project")

    def analyze(est):
        F.set_preferred_estimator(est)
        p = Project.open(take, guide, project_dir=directory)
        assert p.analysis_cached() in (True, False)
        p.analyze(auto_lyrics=False)
        return p

    p = analyze("praat")
    assert calls == ["praat", "praat"]
    assert p.take_f0.estimator == "praat" and p.analysis["take"]["estimator"] == "praat"
    assert p.analysis["take"]["estimator_version"] == F.estimator_version("praat")

    p = analyze("praat")                         # 同じ方式: 読むだけ
    assert calls == ["praat", "praat"]
    assert p.analysis_cached()

    F.set_preferred_estimator("gliss")
    p = Project.open(take, guide, project_dir=directory)
    assert not p.analysis_cached()               # 別の方式の解析しか無い
    p.analyze(auto_lyrics=False)
    assert calls == ["praat", "praat", "gliss", "gliss"]
    assert p.take_f0.estimator == "gliss" and p.guide_f0.estimator == "gliss"
    assert "estimator_version" in p.analysis["take"]

    p = analyze("praat")                         # 戻す: テイクは解析し直し、ガイドは鍵付きの保存を読む
    assert calls == ["praat", "praat", "gliss", "gliss", "praat"]
    assert p.take_f0.estimator == "praat" and p.guide_f0.estimator == "praat"
    assert len(os.listdir(os.path.join(directory, "cache", "guide", "analysis"))) == 2

    p.analyze(estimator="gliss", auto_lyrics=False)   # 名前を指定した解析も、別の方式なら解析し直す
    assert p.take_f0.estimator == "gliss"
    assert calls[-1] == "gliss"


def test_mcp_set_f0_estimator_and_analyze_take(tmp_path, monkeypatch):
    """MCP: set_f0_estimator で選んだ方式で analyze_take が解析し直す。estimator の指定・知らない名前。"""
    from vocal_engine import mcp_server as m
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    m._state.update(project=None, session=None, track=None)
    take = str(tmp_path / "take.wav")
    sf.write(take, _tone(), SR)
    r = m.open_project(take, project_dir=str(tmp_path / "project"))
    assert r["ok"], r
    try:
        r = m.set_f0_estimator("praat")
        assert r["ok"] and r["estimator"] == "praat" and r["effective"] == "praat"
        assert m.engine_info()["f0_estimator"] == "praat"
        r = m.analyze_take(background=False)
        assert r["ok"] and r["f0"]["estimator"] == "praat", r
        assert _cents(r["f0"]["median_hz"], 220.0) < 300      # 2 つの音の中央値（220 Hz と 293.7 Hz の間）
        r = m.analyze_take(estimator="gliss", background=False)
        assert r["ok"] and r["f0"]["estimator"] == "gliss", r
        r = m.analyze_take(background=False)                   # 選んでいる方式（praat）に戻る
        assert r["f0"]["estimator"] == "praat"
        assert m.analyze_take(estimator="nope", background=False)["ok"] is False
        assert m.set_f0_estimator("nope")["ok"] is False
    finally:
        m.set_f0_estimator("rmvpe")
        m._state.update(project=None, session=None, track=None)
