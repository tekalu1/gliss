# -*- coding: utf-8 -*-
"""`export_wav` — 元と同じ形式・長さ・開始位置で、編集区間だけ差し替える。

いちばん大事なのは **「編集していないところは元のファイルと 1 サンプルも違わない」**
（「非破壊で原音を保つ」を、書き出したファイルでも守る）。
整数のまま突き合わせて差分 0 を確かめる。
"""
import os

import numpy as np
import pytest
import soundfile as sf

from conftest import TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


@pytest.fixture
def proj(tmp_path):
    from vocal_engine.project import Project
    p = Project.open(TAKE, project_dir=str(tmp_path / "proj"))
    p.ensure_analyzed()
    return p


def _add(p, kind, t0, t1, params, label=None):
    from vocal_engine.project.model import Target
    return p.apply_edits([{"kind": kind, "target": Target.range(t0, t1),
                           "params": params}], author="human", label=label)


def _ints(path):
    x, sr = sf.read(path, dtype="int32", always_2d=True)
    return x, sr


def test_export_matches_source_outside_edits(proj, tmp_path):
    """ピッチ編集 1 件 + 移動 1 件。**編集区間の外は差分 0**。"""
    from vocal_engine.render.export import export_wav
    _add(proj, "pitch_shift", 0.65, 0.86, {"cents": 100.0}, "+1 半音")
    _add(proj, "move", 3.04, 3.35, {"ms": -60.0}, "60 ms 前へ")

    out = str(tmp_path / "out.wav")
    r = export_wav(proj, path=out)
    assert r["path"] == os.path.abspath(out)
    assert r["backend"] == "praat"

    src = sf.info(TAKE)
    dst = sf.info(out)
    # 元と同じ sr / ch / ビット深度 / 総サンプル数（= 長さも開始位置も同じ）
    assert (dst.samplerate, dst.channels, dst.subtype, dst.frames) == \
           (src.samplerate, src.channels, src.subtype, src.frames)
    assert r["same_as_source"] == {"sr": True, "channels": True, "subtype": True,
                                   "frames": True}
    assert r["warnings"] == []

    a, sr = _ints(TAKE)
    b, _ = _ints(out)
    assert a.shape == b.shape

    mask = np.ones(len(a), dtype=bool)
    for s, e in r["replaced_spans_sec"]:
        mask[int(round(s * sr)):int(round(e * sr))] = False
    assert mask.sum() > 0
    # ---- ここが本題: 差し替えた区間の外は 1 サンプルも違わない
    assert int((a[mask] != b[mask]).sum()) == 0
    # 差し替えた区間は実際に変わっている
    assert int((a[~mask] != b[~mask]).sum()) > 0
    assert r["replaced_sec"] < src.frames / src.samplerate


def test_export_without_edits_is_a_byte_copy(proj, tmp_path):
    """編集が無ければ中身はそのまま（差し替える窓が 0 個）。"""
    from vocal_engine.render.export import export_wav
    out = str(tmp_path / "copy.wav")
    r = export_wav(proj, path=out)
    assert r["replaced_spans_sec"] == []
    a, _ = _ints(TAKE)
    b, _ = _ints(out)
    assert int((a != b).sum()) == 0


def test_export_keeps_length_when_stretching(proj, tmp_path):
    """伸縮しても**総サンプル数は変わらない**（差は窓の末尾の無音が吸収する）。"""
    from vocal_engine.render.export import export_wav
    _add(proj, "stretch", 0.65, 0.86, {"ratio": 1.4}, "40% 伸ばす")
    out = str(tmp_path / "stretched.wav")
    r = export_wav(proj, path=out)
    assert sf.info(out).frames == sf.info(TAKE).frames
    assert r["same_as_source"]["frames"] is True
    a, sr = _ints(TAKE)
    b, _ = _ints(out)
    mask = np.ones(len(a), dtype=bool)
    for s, e in r["replaced_spans_sec"]:
        mask[int(round(s * sr)):int(round(e * sr))] = False
    assert int((a[mask] != b[mask]).sum()) == 0


def test_multichannel_export_shares_analysis_and_gain(tmp_path):
    """3 チャンネル [L, R, L + R] でピッチ編集 → 書き出しても ch2 ≈ ch0 + ch1。

    Praat のパルス解析と音量合わせのゲインをモノラル化した音で 1 回だけ決めて全チャンネルで共有すると、
    どのチャンネルにも同じ線形の処理が掛かるので和の関係が保たれる（定位や左右のバランスが動かない）。
    チャンネルごとに独立に解析・ゲインを決めると崩れる。"""
    from vocal_engine.project import Project
    from vocal_engine.render.export import export_wav
    x, sr = sf.read(TAKE, dtype="float64", always_2d=True)
    x = x[:int(4.0 * sr), 0]
    rng = np.random.default_rng(0)
    left = 0.6 * x
    right = 0.3 * np.roll(x, int(0.003 * sr)) + 0.02 * rng.standard_normal(len(x))
    path = str(tmp_path / "multi.wav")
    sf.write(path, np.stack([left, right, left + right], axis=1), sr, subtype="FLOAT")
    p = Project.open(path, project_dir=str(tmp_path / "proj"))
    p.ensure_analyzed()
    _add(p, "pitch_shift", 0.65, 0.86, {"cents": 200.0})
    out = str(tmp_path / "out.wav")
    r = export_wav(p, path=out)
    assert r["backend"] == "praat" and r["channels"] == 3
    y, _ = sf.read(out, dtype="float64", always_2d=True)
    s, e = r["replaced_spans_sec"][0]
    i, j = int(round(s * sr)), int(round(e * sr))
    assert np.max(np.abs(y[i:j, 0] - left[i:j])) > 1e-3                    # 実際に変わっている
    np.testing.assert_allclose(y[i:j, 2], y[i:j, 0] + y[i:j, 1], atol=1e-5)


def test_default_path_is_next_to_source(tmp_path):
    """既定の書き先は元ファイルの隣の `<名前>_ve.wav`（DAW 連携 段階 1。詳しくは test_daw_stage1.py）。"""
    import shutil
    from vocal_engine.project import Project
    from vocal_engine.render.export import default_path, export_wav
    take = shutil.copy(TAKE, str(tmp_path / "take.wav"))
    p = Project.open(take, project_dir=str(tmp_path / "proj"))
    d = default_path(p)
    assert d == str(tmp_path / "take_ve.wav")
    _add(p, "pitch_shift", 0.65, 0.86, {"cents": 50.0})
    r = export_wav(p)
    assert r["path"] == d and os.path.exists(d)
    assert default_path(p) == str(tmp_path / "take_ve(2).wav")    # 上書きしない


def test_export_tool_is_registered():
    from vocal_engine import mcp_server as m
    assert m.export_wav in m.TOOLS
    assert m.render_region in m.TOOLS                  # 区間 → PCM（DAW 連携 段階 0）
    assert m.render_audition in m.TOOLS                # つかんだノートのプレビュー音（#27）
    assert m.inspect_lyrics_score in m.TOOLS
    assert m.import_lyrics in m.TOOLS
    assert m.get_lyrics in m.TOOLS
    assert m.list_utterances in m.TOOLS
    assert m.set_note_syllable in m.TOOLS
    assert m._mcp_asr.transcribe in m.TOOLS and m._mcp_asr.asr_status in m.TOOLS    # 聞き取り（#54）
    assert m.prep_status in m.TOOLS and m.pause_prep in m.TOOLS    # 裏の準備（#63）
    assert m.set_f0_estimator in m.TOOLS
    assert all(f in m.TOOLS for f in m._mcp_ara.TOOLS) and len(m._mcp_ara.TOOLS) == 8    # DAW（ARA）の ara_*
    assert len(m.TOOLS) == 69
