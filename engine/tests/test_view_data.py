# -*- coding: utf-8 -*-
"""export_view_data（画面向け JSON）・redo・外部の書き換えの読み直し。"""
import json
import os

import pytest

from conftest import needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


@pytest.fixture
def analyzed(project):
    project.analyze()
    return project


def _load(project, **kw):
    from vocal_engine.view.export_data import export_view_data
    r = export_view_data(project, **kw)
    assert r["path"].endswith(".json") and os.path.exists(r["path"])
    # 結果そのものに生の配列は入れない（MCP の決まり）
    assert not any(isinstance(v, list) and len(v) > 4 for v in r.values())
    with open(r["path"], encoding="utf-8") as f:
        return r, json.load(f)


def test_export_has_everything_the_screen_needs(analyzed):
    r, d = _load(analyzed)
    assert d["for"] == "ui"
    assert d["waveform"]["n"] > 100 and len(d["waveform"]["min"]) == d["waveform"]["n"]
    assert len(d["f0"]["take_midi"]) == len(d["f0"]["take_edited_midi"])
    assert len(d["f0"]["take_edited_sec"]) == len(d["f0"]["take_midi"])
    assert len(d["f0"]["guide_midi"]) == len(d["f0"]["guide_sec"])   # ガイドはテイク時間
    # ノートごとの小さな波形（blob）の太さ: F0 のフレームと同じ並びの 0〜1
    assert len(d["f0"]["take_env"]) == len(d["f0"]["take_midi"])
    assert len(d["f0"]["guide_env"]) == len(d["f0"]["guide_midi"])
    for key in ("take_env", "guide_env"):
        env = d["f0"][key]
        assert all(v is not None and 0.0 <= v <= 1.0 for v in env)
        assert max(env) == 1.0 and min(env) < 0.05          # 鳴っているところと無音がある
    assert len(d["notes"]) > 0 and any(n["kind"] == "note" for n in d["notes"])
    assert len(d["boundaries"]) >= len(d["notes"])
    assert d["phonemes"]["supported"] is True
    assert d["phonemes"]["has_lyrics"] is True       # 発声区間から推定の読みが入る
    assert any(e.get("origin") == "estimated" for e in d["lyrics_entries"])
    for n in d["notes"]:
        assert set(("id", "start_sec", "end_sec", "edited_start_sec", "edited_end_sec",
                    "pitch_midi", "edited_pitch_midi", "pitch_editable", "text")) <= set(n)
        assert n["pitch_editable"] == (n["kind"] == "note")
    assert any(n["text"] for n in d["notes"])


def test_edits_show_up_in_the_export(analyzed):
    from vocal_engine.project.model import Target
    nid = [n.id for n in analyzed.take_notes if n.kind == "note"][2]
    _, before = _load(analyzed)
    b = [n for n in before["notes"] if n["id"] == nid][0]

    analyzed.apply_edits([{"kind": "pitch_shift", "target": Target.note(nid),
                           "params": {"cents": 300.0}}], author="human")
    analyzed.apply_edits([{"kind": "stretch", "target": Target.note(nid),
                           "params": {"ratio": 1.3}}], author="human")
    _, after = _load(analyzed)
    a = [n for n in after["notes"] if n["id"] == nid][0]

    assert a["edited"] is True
    assert abs(a["edited_pitch_midi"] - b["pitch_midi"] - 3.0) < 0.01
    assert a["start_sec"] == b["start_sec"]            # 編集前の値は動かない
    out_len = a["edited_end_sec"] - a["edited_start_sec"]
    assert abs(out_len - (b["end_sec"] - b["start_sec"]) * 1.3) < 0.01
    assert len(after["time_map"]["src_sec"]) == len(after["time_map"]["out_sec"]) >= 3
    assert after["history"]["can_undo"] is True


def test_redo_undoes_the_undo(analyzed):
    from vocal_engine.project.model import Target
    nid = [n.id for n in analyzed.take_notes if n.kind == "note"][0]
    analyzed.apply_edits([{"kind": "pitch_shift", "target": Target.note(nid),
                           "params": {"cents": 100.0}}], author="human")
    analyzed.apply_edits([{"kind": "pitch_shift", "target": Target.note(nid),
                           "params": {"cents": 50.0}}], author="human")
    assert analyzed.can_redo() is False
    analyzed.undo()
    analyzed.undo()
    assert len(analyzed.edits) == 0 and analyzed.can_redo() is True
    analyzed.redo()
    assert len(analyzed.edits) == 1                    # 古い方から戻る
    analyzed.redo()
    assert len(analyzed.edits) == 2 and analyzed.can_redo() is False


def test_outside_writes_are_picked_up(analyzed):
    """外部（Claude Code）が project.json を書き換えたら、編集の前に読み直す。"""
    from vocal_engine.project.model import Target
    nid = [n.id for n in analyzed.take_notes if n.kind == "note"][0]
    analyzed.apply_edits([{"kind": "pitch_shift", "target": Target.note(nid),
                           "params": {"cents": 100.0}}], author="ai")
    assert len(analyzed.edits) == 1

    with open(analyzed.json_path, encoding="utf-8") as f:
        raw = json.load(f)
    raw["changesets"][-1]["undone"] = True             # 別プロセスが undo した体
    with open(analyzed.json_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)

    assert analyzed.reload_if_changed() is True
    assert len(analyzed.edits) == 0
    assert analyzed.reload_if_changed() is False       # 2 回目は読み直さない


def test_envelope_follows_loudness_and_ignores_long_silence():
    """包絡は音量に沿う。曲の大半が無音でも、鳴っている所の基準で 1 になる。"""
    import numpy as np
    from vocal_engine.view.export_data import envelope
    sr = 16000
    t = np.arange(sr * 20) / sr
    x = np.zeros_like(t)
    loud = (t >= 18.0) & (t < 19.0)
    quiet = (t >= 19.0) & (t < 19.5)
    x[loud] = 0.5 * np.sin(2 * np.pi * 220 * t[loud])
    x[quiet] = 0.1 * np.sin(2 * np.pi * 220 * t[quiet])
    times = np.arange(0, 20, 0.01)
    env = envelope(x, sr, times, 0.01)
    at = lambda s: float(env[int(round(s / 0.01))])
    assert at(5.0) == 0.0
    assert at(18.5) > 0.95                               # 19 秒の無音があっても細くならない
    assert 0.15 < at(19.25) < 0.25                       # 振幅 1/5 → 太さ 1/5


def test_envelope_cache_does_not_keep_the_audio_alive():
    """包絡のキャッシュが音声の配列を持ち続けない（プロジェクトを切り替えたら解放される）。"""
    import gc
    import weakref
    import numpy as np
    from vocal_engine.view.export_data import envelope
    x = np.random.default_rng(0).normal(size=16000 * 3)
    ref = weakref.ref(x)
    envelope(x, 16000, np.arange(0, 3, 0.01), 0.01)
    del x
    gc.collect()
    assert ref() is None


# ---------------------------------------------------------------- 帯の高さ（v3 §3）
def test_band_center_is_loudness_weighted_and_skips_unvoiced_and_consonants():
    """帯の高さ = 音量で重みを付けた平均の音程。無声（NaN）と子音のフレームは入れない。"""
    import numpy as np
    from vocal_engine.view.export_data import band_center
    midi = np.array([60.0, 60.0, np.nan, 62.0, 70.0])
    env = np.array([1.0, 1.0, 1.0, 2.0, 1.0])
    cons = np.array([False, False, False, False, True])
    # (60·1 + 60·1 + 62·2) / 4 = 61.0（NaN と子音の 70 は入らない）
    assert band_center(midi, env, 0, 5, cons) == pytest.approx(61.0)
    # 子音を除かなければ 70 も入る
    assert band_center(midi, env, 0, 5) == pytest.approx((60 + 60 + 124 + 70) / 5)
    # 子音だけのノート: 子音も入れる（帯が消えない）
    assert band_center(midi, env, 4, 5, cons) == pytest.approx(70.0)
    # 無声だけ・空の区間は fallback
    assert band_center(midi, env, 2, 3, None, 55.0) == 55.0
    assert band_center(midi, env, 3, 3, None, 55.0) == 55.0
    # 音量が全部 0 なら重みなしの平均
    assert band_center(midi, np.zeros(5), 0, 2) == pytest.approx(60.0)


def test_band_moves_with_the_pitch_edit(analyzed):
    """帯はノートといっしょに動く（ピッチを +300 セント → 帯も +3 半音）。ガイドにも帯の高さがある。"""
    from vocal_engine.project.model import Target
    _, before = _load(analyzed)
    ns = [n for n in before["notes"] if n["kind"] == "note"]
    assert all(n["band_midi"] is not None for n in ns)
    # 平均の音程なので、ノートの揺れの幅（10〜90 パーセンタイル）の近くにある
    for n in ns:
        assert n["lo_midi"] - 1.0 <= n["band_midi"] <= n["hi_midi"] + 1.0
    gs = [g for g in before["guide_notes"] if g["pitch_midi"] is not None]
    assert gs and all(g["band_midi"] is not None for g in gs)
    nid = ns[2]["id"]
    analyzed.apply_edits([{"kind": "pitch_shift", "target": Target.note(nid),
                           "params": {"cents": 300.0}}], author="human")
    _, after = _load(analyzed)
    a = [n for n in after["notes"] if n["id"] == nid][0]
    assert a["band_midi"] - ns[2]["band_midi"] == pytest.approx(3.0, abs=0.01)
