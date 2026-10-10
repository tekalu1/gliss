# -*- coding: utf-8 -*-
"""ピッチ（F0）検出の方式（RMVPE・Gliss の F0 モデル・Praat）。合成音だけで確かめる（素材は要らない）。

- どの方式も 10 ms の格子で F0 を返し、既知の音高に近い。無音は無声
- 方式の選び方（`resolve_estimator`。既定は RMVPE。RMVPE の重みが無ければ Gliss のモデル）
- 曲ごとの方式（選んでいなければ、前に解析した方式のまま。RMVPE の重みを後から取っても解析し直さない）
- 解析のキャッシュが方式で分かれる（方式を替えたら解析し直し、同じ方式なら読むだけ）
"""
import hashlib
import os

import numpy as np
import pytest
import soundfile as sf

from vocal_engine.analysis import f0 as F

from conftest import needs_model
from conftest import needs_material

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
    monkeypatch.setattr(F, "_default", None)
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


def test_resolve_estimator(tmp_path, monkeypatch):
    monkeypatch.delenv(F.ESTIMATOR_ENV, raising=False)
    assert F.DEFAULT_ESTIMATOR == "rmvpe" and F.ESTIMATORS[0] == "rmvpe"
    assert F.chosen_estimator() is None and F.preferred_estimator() == "rmvpe"
    fake = tmp_path / "rmvpe.onnx"
    fake.write_bytes(b"fake")
    monkeypatch.setattr(F, "RMVPE_PATH", str(fake))      # RMVPE の重みがある: 既定の RMVPE
    assert F.resolve_estimator() == "rmvpe"
    assert F.resolve_estimator(recorded="gliss") == "gliss"   # 重みが無いときに Gliss で解析した曲は、取った後もそのまま
    monkeypatch.setattr(F, "RMVPE_PATH", os.path.join(os.path.dirname(__file__), "no-such-rmvpe.onnx"))
    assert F.preferred_estimator() == "rmvpe"
    assert F.resolve_estimator() == "gliss"             # RMVPE の重みが無い: 同梱のモデルで解析する
    assert F.resolve_estimator(recorded="rmvpe") == "gliss"
    F.set_preferred_estimator("rmvpe")
    assert F.resolve_estimator() == "gliss"
    assert F.resolve_estimator("rmvpe") == "rmvpe"      # 名前を指定したときはそのまま
    with pytest.raises(F.ModelMissingError):
        F.estimate_f0(x=_tone(sec=0.5), sr=SR, estimator="rmvpe")
    assert F.set_preferred_estimator("praat") == "praat"
    assert F.resolve_estimator() == "praat"
    with pytest.raises(ValueError):
        F.set_preferred_estimator("nope")
    with pytest.raises(ValueError):
        F.resolve_estimator("nope")
    assert F.resolve_estimator(recorded="gliss") == "praat"   # 選んだ方式は曲の方式より強い
    F.set_preferred_estimator(None)
    assert F.chosen_estimator() is None
    assert F.resolve_estimator(recorded="praat") == "praat"   # 選んでいなければ曲を前に解析した方式
    assert F.resolve_estimator(recorded="fcpe") == "gliss"    # 画面で選べない方式は既定に（RMVPE の重みが無いので Gliss）
    monkeypatch.setenv(F.ESTIMATOR_ENV, "praat")
    assert F.chosen_estimator() == "praat"
    assert F.resolve_estimator() == "praat"
    assert F.resolve_estimator(recorded="gliss") == "praat"
    v = F.estimator_version("gliss")
    assert F.same_estimator("gliss", v, "gliss")
    assert not F.same_estimator("gliss", "proto1", "gliss")      # 前のモデル（試作）の解析: 作り直す
    assert not F.same_estimator("gliss", None, "gliss")          # 版が違う: 作り直す
    assert not F.same_estimator("rmvpe", None, "praat")
    assert F.same_estimator("gliss", v, "auto") and F.same_estimator("rmvpe", None, "auto")


def test_default_estimator_is_weaker_than_the_chosen_and_the_recorded(monkeypatch):
    """`set_default_estimator`（DAW のプラグインの設定）: 方式の決まっていない曲だけの既定。選んだ方式・曲を前に解析した方式が先。"""
    monkeypatch.setattr(F, "RMVPE_PATH", os.path.join(os.path.dirname(__file__), "no-such-rmvpe.onnx"))
    assert F.default_estimator() is None
    assert F.set_default_estimator("praat") == "praat"
    assert F.default_estimator() == "praat" and F.chosen_estimator() is None
    assert F.preferred_estimator() == "praat"
    assert F.resolve_estimator() == "praat"                       # 新しい曲
    assert F.resolve_estimator(recorded="gliss") == "gliss"       # 前に解析した方式が先
    assert F.resolve_estimator("gliss") == "gliss"                # 名前の指定が先
    F.set_preferred_estimator("gliss")
    assert F.resolve_estimator(recorded="praat") == "gliss"       # 利用者が選んだ方式は曲の方式より強い
    F.set_preferred_estimator(None)
    with pytest.raises(ValueError):
        F.set_default_estimator("nope")
    F.set_default_estimator(None)
    assert F.default_estimator() is None and F.preferred_estimator() == "rmvpe"


def test_bundled_model_exists():
    assert os.path.exists(F.GLISS_F0_PATH)
    assert os.path.getsize(F.GLISS_F0_PATH) < 1 << 20
    assert os.path.exists(F.GLISS_F0_V2_PATH)
    with open(F.GLISS_F0_PATH, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == "f7e90f3dbbb41543dff2481d4a49b3fe23bed3deef0617e1844285ec66210ba0"
    with open(F.GLISS_F0_V2_PATH, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest().startswith(F.GLISS_F0_V2_VERSION[2:])
    assert F.estimator_version("gliss") != F.GLISS_F0_V2_VERSION
    assert not F.same_estimator("gliss", F.GLISS_F0_V2_VERSION, "gliss")


def test_v2_project_keeps_note_targets_until_explicit_reanalysis(tmp_path, monkeypatch):
    from vocal_engine.project import Project
    from vocal_engine.project.model import Target

    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    take = str(tmp_path / "synthetic.wav")
    sf.write(take, _tone(), SR)
    directory = str(tmp_path / "project")
    p = Project.open(take, project_dir=directory)
    p.f0_model_version = F.GLISS_F0_V2_VERSION
    p.analyze(estimator="gliss", auto_lyrics=False)
    note = next(n for n in p.take_notes if n.kind == "note")
    p.apply_edits([{"kind": "pitch_shift", "target": Target.note(note.id), "params": {"cents": 90}}])
    before = p.take_f0.f0.copy()
    p = Project.open(take, project_dir=directory)
    assert p.analysis_cached("gliss")
    assert p.take_f0.meta["version"] == F.GLISS_F0_V2_VERSION
    assert np.array_equal(p.take_f0.f0, before)
    assert p._missing_note_targets() == []
    os.remove(os.path.join(directory, "cache", "take-analysis.json"))
    p._forget_analysis()
    p.ensure_analyzed()
    assert p.take_f0.meta["version"] == F.GLISS_F0_V2_VERSION
    assert p._missing_note_targets() == []
    p.analyze(force=True, estimator="gliss", auto_lyrics=False)
    assert p.take_f0.meta["version"] == F.estimator_version("gliss")
    assert p.analysis["take"]["estimator_version"] == F.estimator_version("gliss")


@needs_material("C", "C2")
def test_gliss_v3_material_split_guide_and_psola(tmp_path, monkeypatch):
    """素材は読み取るだけ。新モデルの分割・ガイド対応・再合成を一つの編集で通す。"""
    import materials
    from vocal_engine import mcp_server as m
    from vocal_engine.project import Project, timing as TM
    from vocal_engine.render.region import render_region

    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    m._state.update(project=None, session=None, track=None)
    take, guide = materials.clip("C"), materials.clip("C2")
    try:
        opened = m.open_project(take, guide, project_dir=str(tmp_path / "project"))
        assert opened["ok"], opened
        analyzed = m.analyze_take(estimator="gliss", background=False)
        assert analyzed["ok"], analyzed
        p = m._state["project"]
        assert p.take_f0.meta["version"] == F.estimator_version("gliss")
        pairs, _, _ = TM.note_correspondence(p)
        assert pairs
        note = max((n for n in p.take_notes if n.kind == "note"),
                   key=lambda n: n.end_sec - n.start_sec)
        assert note.end_sec - note.start_sec > 0.08
        split = m.split_note((note.start_sec + note.end_sec) / 2, note_id=note.id)
        assert split["ok"], split
        shifted = m.shift_pitch(100, note_id=split["right"])
        assert shifted["ok"], shifted
        y, info = render_region(p, backend="psola")
        assert len(y) > 0 and np.isfinite(y).all() and info["rendered_windows_sec"]
        original, _ = p.audio("take")
        assert np.max(np.abs(y[:, 0] - original)) > 1e-3
        archive = p.to_archive()
        assert archive["f0_estimator_version"] == F.estimator_version("gliss")
        q = Project.from_archive(archive, take=take, guide=guide,
                                 project_dir=str(tmp_path / "reopened"))
        q.ensure_analyzed()
        assert q.take_f0.meta["version"] == F.estimator_version("gliss")
        assert q._missing_note_targets() == []
        assert any(n.id == split["right"] for n in q.take_notes)
        restored, _ = render_region(q, backend="psola")
        assert np.array_equal(restored, y)
    finally:
        m._state.update(project=None, session=None, track=None)


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
        r = m.analyze_take(background=False)                   # 明示した方式はそのトラックの方式として残る
        assert r["f0"]["estimator"] == "gliss"
        assert m.set_f0_estimator("praat")["ok"]               # 選び直すと全体の方式（praat）に戻る
        r = m.analyze_take(background=False)
        assert r["f0"]["estimator"] == "praat"
        assert m.analyze_take(estimator="nope", background=False)["ok"] is False
        assert m.set_f0_estimator("nope")["ok"] is False
    finally:
        F.set_preferred_estimator(None)
        m._state.update(project=None, session=None, track=None)


def test_project_keeps_the_estimator_it_was_analyzed_with(tmp_path, monkeypatch):
    """方式を選んでいなければ、前に解析した曲は前の方式のまま（音符の区切り・付けた編集の当たり方を変えない）。
    新しい曲は既定（RMVPE。重みが無ければ Gliss）。RMVPE の重みを後から取っても、Gliss で解析した曲は Gliss のまま。
    利用者が選んだ方式は曲の方式より強い。"""
    from vocal_engine.project import Project, store
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    monkeypatch.delenv(F.ESTIMATOR_ENV, raising=False)
    monkeypatch.setattr(F, "RMVPE_PATH", str(tmp_path / "no-such-rmvpe.onnx"))    # 初回に RMVPE を取らなかった
    assert store.recorded_estimator({}) is None
    assert store.recorded_estimator({"take": {"n_frames": 10}}) == "rmvpe"   # 方式を記録する前の解析
    assert store.recorded_estimator({"take": {"estimator": "praat"}}) == "praat"
    calls = []
    real = store.estimate_f0

    def counting(x, sr, estimator=None, sweep=False):
        calls.append(estimator)
        return real(x=x, sr=sr, estimator=estimator, sweep=sweep)

    monkeypatch.setattr(store, "estimate_f0", counting)
    take = str(tmp_path / "take.wav")
    sf.write(take, _tone(), SR)
    old = str(tmp_path / "old")

    # 別の方式で解析した曲に見立てる: 方式を指定して解析する
    p = Project.open(take, project_dir=old)
    p.analyze(estimator="praat", auto_lyrics=False)
    assert p.analysis["take"]["estimator"] == "praat"
    assert store.recorded_estimator_in(old) == "praat"

    p = Project.open(take, project_dir=old)      # 方式を選んでいない: 前の方式のまま、読むだけ
    assert p.f0_estimator() == "praat"
    assert p.analysis_cached()
    p.analyze(auto_lyrics=False)
    assert calls == ["praat"] and p.take_f0.estimator == "praat"

    new = str(tmp_path / "new")
    q = Project.open(take, project_dir=new)      # 新しい曲: 既定の RMVPE だが重みが無いので Gliss
    assert q.f0_estimator() == "gliss"
    q.analyze(auto_lyrics=False)
    assert calls == ["praat", "gliss"] and q.take_f0.estimator == "gliss"

    fake = tmp_path / "rmvpe.onnx"               # 後から RMVPE の重みを取った
    fake.write_bytes(b"fake")
    monkeypatch.setattr(F, "RMVPE_PATH", str(fake))
    assert Project.open(take, project_dir=new).f0_estimator() == "gliss"      # Gliss で解析した曲はそのまま
    assert Project.open(take, project_dir=old).f0_estimator() == "praat"
    assert Project.open(take, project_dir=str(tmp_path / "newer")).f0_estimator() == "rmvpe"   # 新しい曲は RMVPE
    monkeypatch.setattr(F, "RMVPE_PATH", str(tmp_path / "no-such-rmvpe.onnx"))

    F.set_preferred_estimator("gliss")           # 利用者が選んだ: 前の曲もその方式で解析し直す
    p = Project.open(take, project_dir=old)
    assert p.f0_estimator() == "gliss" and not p.analysis_cached()
    p.analyze(auto_lyrics=False)
    assert calls == ["praat", "gliss", "gliss"]
    assert store.recorded_estimator_in(old) == "gliss"


def test_mcp_keeps_the_estimator_per_project(tmp_path, monkeypatch):
    """MCP: 方式を選んでいなければ、analyze_take は曲を前に解析した方式で読む。engine_info の effective も曲の方式。"""
    from vocal_engine import mcp_server as m
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    m._state.update(project=None, session=None, track=None)
    take = str(tmp_path / "take.wav")
    sf.write(take, _tone(), SR)
    try:
        assert m.open_project(take, project_dir=str(tmp_path / "project"))["ok"]
        r = m.analyze_take(estimator="praat", background=False)
        assert r["ok"] and r["f0"]["estimator"] == "praat", r
        r = m.analyze_take(background=False)                   # 選んでいない: この曲の方式のまま
        assert r["f0"]["estimator"] == "praat"
        info = m.engine_info()
        assert info["f0_estimator"] == "rmvpe" and info["f0_estimator_chosen"] is None
        assert info["f0_estimator_effective"] == "praat"
        assert m.open_project(take, project_dir=str(tmp_path / "project"))["ok"]   # 開き直しても同じ
        assert m.analyze_take(background=False)["f0"]["estimator"] == "praat"
        r = m.set_f0_estimator("gliss")                         # 選ぶと、この曲もその方式に
        assert r["ok"] and r["effective"] == "gliss" and r["changed"]
        assert m.analyze_take(background=False)["f0"]["estimator"] == "gliss"
    finally:
        F.set_preferred_estimator(None)
        m._state.update(project=None, session=None, track=None)


def test_gliss_version_follows_model_file(tmp_path, monkeypatch):
    """Gliss の方式の版は、モデルファイルの SHA-256 から決まる（モデルを替えたら前の解析を使い回さない）。"""
    import hashlib
    v = F.estimator_version("gliss")
    with open(F.GLISS_F0_PATH, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    assert v == "m-" + sha[:12]
    other = tmp_path / "other.onnx"
    other.write_bytes(b"another model")
    monkeypatch.setattr(F, "GLISS_F0_PATH", str(other))
    v2 = F.estimator_version("gliss")
    assert v2 != v and v2.startswith("m-")
    assert not F.same_estimator("gliss", v, "gliss") and F.same_estimator("gliss", v2, "gliss")
    other.write_bytes(b"third model!!")                           # 同じ場所で中身が変わっても取り直す
    assert F.estimator_version("gliss") not in (v, v2)
    assert F.estimator_version("praat") == "1" and F.estimator_version("rmvpe") is None
