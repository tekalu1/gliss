# -*- coding: utf-8 -*-
"""ノートのフェード（issue #20）と、テンポ・グリッド（issue #18）のエンジン側。

フェード:
- set_fade: 音量だけ（フェードの区間の中は 元 × 等パワーの包絡、外は元のサンプルのまま。隣のノートは変わらない）
- view data / list_notes の fade_in_sec・fade_out_sec、取り消しの名前「フェード」、両方 0 で「フェードを消す」
- ノートより長いフェードは比を保って縮める・ノートの長さを変えても秒は保つ・分割でイン は左／アウトは右
- ピッチを動かしたノートにも掛かる・オリジナルに戻すで外れる・再生（render_region）も書き出しと同じ中身
テンポ:
- iXML（PreSonus の TEMPO_MAP）の読み方（1 拍の秒 → BPM、段の OFFSET はサンプル、1 小節目は bext から）
- トラックを足したら自動で読む（取り消すとトラックと一緒に消える）・手入力（set_tempo）・group で 1 回にまとめる
"""
import json
import os
import shutil

import numpy as np
import pytest
import soundfile as sf

from conftest import CLIP_E, GUIDE, TAKE, needs_clips, needs_model

pytestmark = [needs_clips]


@pytest.fixture
def mcp():
    from vocal_engine import mcp_server as m
    yield m
    m._state.update(project=None, session=None, track=None)
    m._invalidate_renderer()


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _open(m, d):
    _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    _ok(m.analyze_take())
    return m._state["project"]


def _view(p):
    from vocal_engine.view.export_data import export_view_data
    with open(export_view_data(p)["path"], encoding="utf-8") as f:
        return json.load(f)


def _export(p, out):
    from vocal_engine.render.export import export_wav
    export_wav(p, path=str(out))
    a, sr = sf.read(TAKE, dtype="float64", always_2d=True)
    b, _ = sf.read(str(out), dtype="float64", always_2d=True)
    assert a.shape == b.shape
    return a, b, sr


def _pick(p, k=0):
    ns = [n for n in p.take_notes if n.kind == "note" and n.end_sec - n.start_sec > 0.25]
    return ns[len(ns) // 2 + k]


# ================================================================ フェード
@needs_model
def test_fade_changes_only_the_volume_inside(tmp_path, mcp):
    m = mcp
    p = _open(m, str(tmp_path / "f1"))
    n = _pick(p)
    fi, fo = 0.08, 0.12
    r = _ok(m.set_fade(note_ids=[n.id], fade_in_sec=fi, fade_out_sec=fo, author="human"))
    assert r["history"]["undo"]["label"] == "フェード"
    assert r["fades"][n.id] == [fi, fo]
    a, b, sr = _export(p, tmp_path / "out.wav")
    s0, s1 = int(round(n.start_sec * sr)), int(round(n.end_sec * sr))
    # フェードの外は元のサンプルそのまま（ノートの中ほども、前後のノートも）
    diff = np.flatnonzero(np.any(np.abs(a - b) > 1e-9, axis=1))
    assert diff.min() >= s0 - 1 and diff.max() <= s1 + 1
    mid = slice(int(round((n.start_sec + fi) * sr)) + 2, int(round((n.end_sec - fo) * sr)) - 2)
    assert np.array_equal(a[mid], b[mid])
    # 中は 元 × 包絡（イン sin・アウト cos。量子化の誤差だけ）
    i0, m_ = s0, int(round(fi * sr))
    u = (np.arange(m_) + 0.5) / m_
    want = a[i0:i0 + m_, 0] * np.sin(0.5 * np.pi * u)
    assert np.max(np.abs(b[i0:i0 + m_, 0] - want)) < 2e-4
    assert np.max(np.abs(b[i0:i0 + int(0.004 * sr), 0])) < 0.1 * max(1e-9, np.max(np.abs(a[i0:i0 + m_, 0])))
    o1 = s1
    o0 = o1 - int(round(fo * sr))
    u = (np.arange(o1 - o0) + 0.5) / (o1 - o0)
    assert np.max(np.abs(b[o0:o1, 0] - a[o0:o1, 0] * np.cos(0.5 * np.pi * u))) < 2e-4
    # 画面のデータ・list_notes
    by = {x["id"]: x for x in _view(p)["notes"]}
    assert by[n.id]["fade_in_sec"] == pytest.approx(fi) and by[n.id]["fade_out_sec"] == pytest.approx(fo)
    assert by[n.id]["edited"] is True
    others = [x for x in by.values() if x["id"] != n.id]
    assert all(x["fade_in_sec"] == 0 and x["fade_out_sec"] == 0 for x in others)
    ln = {x["id"]: x for x in _ok(m.list_notes())["notes"]}
    assert ln[n.id]["fade_in_sec"] == pytest.approx(fi)
    # 取り消す → 元に戻る
    u = _ok(m.undo())
    assert u["undone"]["label"] == "フェード"
    a, b, _ = _export(p, tmp_path / "back.wav")
    assert np.array_equal(a, b)


@needs_model
def test_fade_clear_clamp_split_and_reset(tmp_path, mcp):
    m = mcp
    p = _open(m, str(tmp_path / "f2"))
    n = _pick(p)
    L = n.end_sec - n.start_sec
    # ノートより長いフェード: 比を保ってノートの長さに収める
    _ok(m.set_fade(note_ids=[n.id], fade_in_sec=L, fade_out_sec=L, author="human"))
    d = {x["id"]: x for x in _view(p)["notes"]}[n.id]
    assert d["fade_in_sec"] + d["fade_out_sec"] == pytest.approx(L, abs=1e-4)
    assert d["fade_in_sec"] == pytest.approx(L / 2, abs=1e-4)
    # 片側だけ変える（もう片方はそのまま）・同じ値は何もしない
    _ok(m.set_fade(note_ids=[n.id], fade_in_sec=0.05, author="human"))
    d = {x["id"]: x for x in _view(p)["notes"]}[n.id]
    assert d["fade_in_sec"] == pytest.approx(0.05) and d["fade_out_sec"] == pytest.approx(L / 2, abs=1e-4)
    # もう片側がある: 片側はノートの長さ − もう片側まで
    _ok(m.set_fade(note_ids=[n.id], fade_in_sec=L, author="human"))
    d = {x["id"]: x for x in _view(p)["notes"]}[n.id]
    assert d["fade_in_sec"] == pytest.approx(L / 2, abs=1e-4)
    _ok(m.set_fade(note_ids=[n.id], fade_in_sec=0.05, author="human"))
    assert _ok(m.set_fade(note_ids=[n.id], fade_in_sec=0.05, author="human"))["changeset"] is None
    _ok(m.set_fade(note_ids=[n.id], fade_out_sec=0.06, author="human"))
    # 分割: イン は左の片、アウトは右の片に残る
    mid = round((n.start_sec + n.end_sec) / 2, 4)
    _ok(m.split_note(sec=mid, note_id=n.id, author="human"))
    notes = [x for x in _view(p)["notes"] if x["kind"] == "note"]
    left = next(x for x in notes if abs(x["start_sec"] - n.start_sec) < 1e-3)
    right = next(x for x in notes if abs(x["end_sec"] - n.end_sec) < 1e-3)
    assert left["id"] != right["id"]
    assert left["fade_in_sec"] == pytest.approx(0.05) and left["fade_out_sec"] == 0
    assert right["fade_in_sec"] == 0 and right["fade_out_sec"] == pytest.approx(0.06)
    _ok(m.undo())                                            # 分割を戻す
    # 両方 0 = フェードを消す
    r = _ok(m.set_fade(note_ids=[n.id], fade_in_sec=0, fade_out_sec=0, author="human"))
    assert r["history"]["undo"]["label"] == "フェードを消す"
    assert not any(e.kind == "fade" for e in p.edits)
    _ok(m.undo())
    assert any(e.kind == "fade" for e in p.edits)
    # オリジナルに戻す: そのノートのフェードも外れる（隣のノートのフェードは残る）
    nb = next(x for x in p.take_notes if x.kind == "note" and x.id != n.id and x.end_sec - x.start_sec > 0.1)
    _ok(m.set_fade(note_ids=[nb.id], fade_in_sec=0.04, author="human"))
    _ok(m.reset_to_original(note_ids=[n.id], author="human"))
    fes = [e for e in p.edits if e.kind == "fade"]
    assert len(fes) == 1 and fes[0].params["note_id"] == nb.id
    # 音程のないノート・無いノートは断る
    bad = m.set_fade(note_ids=["n999"], fade_in_sec=0.1)
    assert bad["ok"] is False and "n999" in bad["error"]
    assert m.set_fade(note_ids=[nb.id])["ok"] is False


@needs_model
def test_fade_keeps_seconds_when_the_note_is_lengthened(tmp_path, mcp):
    m = mcp
    p = _open(m, str(tmp_path / "f3"))
    n = _pick(p)
    _ok(m.set_fade(note_ids=[n.id], fade_in_sec=0.05, fade_out_sec=0.07, author="human"))
    plan = _ok(m.plan_edit(op="edge", note_id=n.id, side="end", detach=True))
    with open(plan["path"], encoding="utf-8") as f:
        data = json.load(f)
    lo, hi = data["x_range"]
    x = max(-0.03, lo) if lo is not None else -0.03
    _ok(m.apply_plan(plan_id=data["plan_id"], x=x, author="human"))
    d = {q["id"]: q for q in _view(p)["notes"]}[n.id]
    assert d["edited_end_sec"] - d["edited_start_sec"] == pytest.approx(n.end_sec - n.start_sec + x, abs=2e-3)
    assert d["fade_in_sec"] == pytest.approx(0.05) and d["fade_out_sec"] == pytest.approx(0.07)


@needs_model
def test_fade_on_pitch_shifted_note_and_playback_matches_export(tmp_path, mcp):
    m = mcp
    p = _open(m, str(tmp_path / "f4"))
    n = _pick(p)
    _ok(m.shift_pitch(cents=150, note_id=n.id, author="human"))
    _ok(m.set_fade(note_ids=[n.id], fade_in_sec=0.06, author="human"))
    a, b, sr = _export(p, tmp_path / "o.wav")
    s0 = int(round(n.start_sec * sr))
    k = int(0.004 * sr)
    ref = np.max(np.abs(b[s0 + int(0.06 * sr):s0 + int(0.12 * sr), 0]))
    assert np.max(np.abs(b[s0:s0 + k, 0])) < 0.15 * ref           # 頭は 0 から
    # 再生（render_region）も書き出しと同じ中身
    from vocal_engine.render.region import render_region
    y, info = render_region(p, 0.0, p.duration_sec, channels="all")
    assert y.shape[0] == b.shape[0]
    assert np.max(np.abs(y[:, 0] - b[:, 0])) < 1e-4


# ================================================================ テンポ（iXML）
def _ixml(offsets_values):
    segs = "".join("<TEMPO_SEGMENT><TEMPO_SEGMENT_OFFSET>%d</TEMPO_SEGMENT_OFFSET>"
                   "<TEMPO_SEGMENT_VALUE>%.20f</TEMPO_SEGMENT_VALUE></TEMPO_SEGMENT>" % (o, v)
                   for o, v in offsets_values)
    return ('﻿<?xml version="1.0" encoding="UTF-8"?>\n<BWFXML>\n\t<PRESONUS>\n\t\t<TEMPO_MAP>%s'
            '</TEMPO_MAP>\n\t</PRESONUS>\n</BWFXML>' % segs).encode("utf-8")


def _wav_with_tempo(src, dst, bpm, tref_sec=None, change_to=None):
    """src を dst に写して、bext（TimeReference）と iXML のテンポマップを入れる。"""
    from vocal_engine import bwf
    x, sr = sf.read(src, dtype="float32", always_2d=True)
    sf.write(dst, x, sr, subtype="PCM_24")
    segs = [(0, 60.0 / bpm), (2835, 60.0 / bpm + 1e-8), (14835, 60.0 / bpm + 2e-8)]
    if change_to:
        segs += [(26835, 60.0 / change_to)]
    meta = bwf.BwfMeta(bext=bwf.minimal_bext(int(round((tref_sec or 0) * sr))) if tref_sec is not None
                       else None, ixml=_ixml(segs))
    bwf.write_meta(dst, meta)
    return sr


def test_parse_tempo_map_reads_presonus_ixml(tmp_path):
    from vocal_engine import bwf
    segs = bwf.parse_tempo_map(_ixml([(0, 0.35087944688833094), (2835, 0.35087945907164530)]))
    assert len(segs) == 2 and segs[0][0] == 0 and segs[1][0] == 2835
    assert segs[0][1] == pytest.approx(171.0, abs=0.01)          # Studio One の実物の値
    assert bwf.parse_tempo_map(b"<BWFXML><PRESONUS></PRESONUS></BWFXML>") == []
    assert bwf.parse_tempo_map(None) == []
    dst = str(tmp_path / "Inst_mix.wav")
    sr = _wav_with_tempo(CLIP_E, dst, 171.0, tref_sec=46.315937, change_to=175.0)
    t = bwf.read_tempo(dst)
    assert t["bpm"] == 171.0 and t["varies"] is True and t["bpm_range"] == [171.0, 175.0]
    assert t["time_reference_sec"] == pytest.approx(46.315937, abs=1.0 / sr)
    assert bwf.read_tempo(CLIP_E) is None                     # iXML の無い WAV


@needs_model
def test_tempo_from_ixml_when_adding_a_track_and_undo(tmp_path, mcp):
    m = mcp
    from vocal_engine import mcp_tracks as mt
    _open(m, str(tmp_path / "t1"))
    s0 = _ok(mt.list_tracks())
    assert s0["tempo"] is None
    dst = str(tmp_path / "Inst_mix.wav")
    _wav_with_tempo(CLIP_E, dst, 155.0, tref_sec=1.548396)
    r = _ok(mt.add_track(dst, offset_sec=0.5, author="human"))
    t = r["session"]["tempo"]
    assert t["source"] == "ixml" and t["bpm"] == 155.0 and (t["num"], t["den"]) == (4, 4)
    assert t["start_sec"] == pytest.approx(0.5 - 1.548396, abs=1e-4)   # ファイルの頭 − TimeReference
    assert t["file"] == "Inst_mix.wav" and t["varies"] is False
    # 取り消すとトラックと一緒にテンポも消える（トラックの追加の 1 回）
    u = _ok(m.undo())
    assert u["undone"]["label"] == "トラックの追加" and u["session"]["tempo"] is None
    rd = _ok(m.redo())
    assert rd["session"]["tempo"]["bpm"] == 155.0


@needs_model
def test_set_tempo_manual_group_and_undo(tmp_path, mcp):
    m = mcp
    from vocal_engine import mcp_tracks as mt
    _open(m, str(tmp_path / "t2"))
    r = _ok(mt.set_tempo(bpm=128, author="human"))
    t = r["tempo"]
    assert (t["bpm"], t["num"], t["den"], t["start_sec"], t["source"]) == (128.0, 4, 4, 0.0, "manual")
    assert r["history"]["undo"]["label"] == "テンポ"
    # 同じ group は 1 回にまとまる（ドラッグ・続けたホイール）
    for v in (129, 130, 131):
        _ok(mt.set_tempo(bpm=v, group="w1", author="human"))
    h = _ok(mt.list_tracks())["history"]
    assert h["undo"]["label"] == "テンポ"
    u = _ok(m.undo())
    assert u["session"]["tempo"]["bpm"] == 128.0              # 129〜131 がまとめて戻る
    # まとめた結果が元と同じなら項目は残らない
    _ok(mt.set_tempo(bpm=140, group="w2", author="human"))
    _ok(mt.set_tempo(bpm=128, group="w2", author="human"))
    assert _ok(mt.list_tracks())["history"]["undo"]["label"] == "テンポ"
    u = _ok(m.undo())
    assert u["session"]["tempo"] is None                      # 最初の set_tempo(128) が戻った
    # 拍子・1 小節目
    r = _ok(mt.set_tempo(numerator=3, denominator=4, start_sec=0.25, author="human"))
    assert r["tempo"]["num"] == 3 and r["tempo"]["start_sec"] == 0.25 and r["tempo"]["bpm"] == 120.0
    assert r["history"]["undo"]["label"] == "テンポと拍子"
    r = _ok(mt.set_tempo(numerator=6, denominator=8, author="human"))
    assert (r["tempo"]["num"], r["tempo"]["den"]) == (6, 8) and r["history"]["undo"]["label"] == "拍子"
    _ok(m.undo())
    for bad in ({"bpm": 5}, {"numerator": 0}, {"denominator": 3}):
        assert mt.set_tempo(**bad)["ok"] is False
    r = _ok(mt.set_tempo(clear=True, author="human"))
    assert r["tempo"] is None and r["history"]["undo"]["label"] == "テンポを消す"
    # セッションに残る（開き直しても同じ）
    _ok(m.undo())
    d = m._state["session"].dir
    with open(os.path.join(d, "session.json"), encoding="utf-8") as f:
        assert json.load(f)["tempo"]["num"] == 3
