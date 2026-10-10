# -*- coding: utf-8 -*-
"""直したノートの付近だけを作り直す（`render/region.py` の `plan_update`・`pipeline.clean_cuts`。2026-10-10 承認）。

窓（編集のかたまり。`export.py` の `_windows`）は、ノートが詰まった曲では数十秒になる。1 音を直すたびに窓を丸ごと
作り直さず、窓の中の「区切れる点」（原音のままで位置がずれていない隙間の中）で切って、付近だけを作る。
区切れる点で切った音は、窓を丸ごと作った音とサンプル単位で同じ（つなぎのクロスフェードは要らない）。

- 区切れる点で切った切れ端は、窓を丸ごと作った音と同じ（praat・psola、モノラル・ステレオ、時間を動かす編集を含む）。
  ノートの中で切ると違う（判定が要る）
- `EditCache`: 1 音のピッチなら作り直すのは 1 秒未満（窓は 8 秒超）。編集・取り消しのたびに全体と同じ。
  取り消すと元の PCM に戻る。描画の版 1 と 2
- `ara_render_dirty`: 同じことを プラグインのキャッシュの真似で。`max_sec` で分けても同じ。試聴に残す PCM も今の音と同じ
- 素材（S3_M1）で同じこと
"""
import os

import numpy as np
import pytest
import soundfile as sf

from conftest import needs_material, needs_model
from test_ara_tools import Cache, _add, _ok, _open, _same, _select_and_analyze, _wav, ara  # noqa: F401

SR = 44100
# 0.15 秒あけて 14 音。かたまりが 1 つの長い窓（約 10 秒）になる
NOTES = [(0.3 + i * 0.7, 0.55, 60 + (i * 5) % 12) for i in range(14)]
DUR = NOTES[-1][0] + 1.2


def _voice(stereo=False):
    n = int(SR * DUR)
    y = np.zeros(n)
    for st, d, midi in NOTES:
        a, b = int(st * SR), int((st + d) * SR)
        t = np.arange(b - a) / SR
        f0 = 440.0 * 2 ** ((midi - 69) / 12)
        vib = 30 * np.sin(2 * np.pi * 5.5 * t) * np.clip((t - 0.12) / 0.1, 0, 1)
        ph = 2 * np.pi * np.cumsum(f0 * 2 ** (vib / 1200)) / SR
        s = sum((0.6 ** k) * np.sin((k + 1) * ph) for k in range(8))
        env = np.minimum(1, np.minimum(t / 0.03, (d - t) / 0.04))
        y[a:b] += 0.25 * s * env
    y += np.random.default_rng(0).normal(scale=1e-4, size=n)
    return np.stack([y, 0.8 * np.roll(y, 3)], axis=1) if stereo else y


@pytest.fixture
def praat(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m
    from vocal_engine.analysis import f0 as F
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    F.set_preferred_estimator("praat")
    yield m
    F.set_preferred_estimator(None)
    m._state["project"] = None
    m._invalidate_renderer()


def _segs_pitch():
    from vocal_engine.render.pipeline import Segment
    return [Segment(st, st + d, cents=100 - 20 * i) for i, (st, d, _m) in enumerate(NOTES)]


def _segs_variety():
    """ピッチ・伸縮・曲線・移動・ミュート・無音の挿入と切り取り・フェードの印。"""
    from vocal_engine.render.pipeline import Segment
    out = []
    for i, (st, d, _m) in enumerate(NOTES):
        k = i % 7
        if k == 0:
            out.append(Segment(st, st + d, cents=120))
        elif k == 1:
            out.append(Segment(st, st + d, ratio=1.15))
        elif k == 2:
            out.append(Segment(st, st + d, cents=-80, curve_points=[[0.0, 0.0], [0.2, 60.0], [0.5, -40.0]]))
        elif k == 3:
            out.append(Segment(st, st + d, move_ms=40.0))
            out.append(Segment(st + d + 0.05, st + d + 0.1, cents=30))
        elif k == 4:
            out.append(Segment(st, st + d, gain=0.0))
        elif k == 5:
            out.append(Segment(st + 0.1, st + 0.1, silence_sec=0.08))
            out.append(Segment(st + d - 0.08, st + d, ratio=0.0))
        else:
            out.append(Segment(st, st + d, cents=50))
            out.append(Segment(st, st + 0.2, fade=(st, st + 0.2, "in")))
    return out


@pytest.mark.parametrize("backend,stereo", [("praat", True), ("psola", False)])
def test_cuts_at_clean_points_keep_the_sound_of_the_whole_window(praat, tmp_path, backend, stereo):
    from vocal_engine.project import Project
    from vocal_engine.render.pipeline import clean_cuts
    from vocal_engine.render.region import RegionRenderer
    path = _wav(tmp_path / "src" / "v.wav", _voice(stereo))
    p = Project.open(path, project_dir=str(tmp_path / "p"))
    p.ensure_analyzed()
    x = sf.read(path, dtype="float64", always_2d=True)[0]
    rr = RegionRenderer(x, SR, p.take_f0, backend=backend)
    wa, wb = int(round(0.1 * SR)), int(round((DUR - 0.2) * SR))
    for name, segs in (("pitch", _segs_pitch()), ("variety", _segs_variety())):
        full, _ = rr.render_frames(wa, wb, segs)
        cuts = clean_cuts(segs, wa / SR, wb / SR, SR, len(x))
        assert cuts[0][0] == wa and cuts[-1][1] == wb
        pts = sorted({c for a, b in cuts for c in (a, (a + b) // 2, b)})
        assert len(pts) > 6, name
        for i in range(len(pts) - 1):
            c1, c2 = pts[i], pts[min(len(pts) - 1, i + 3)]
            if c2 - c1 < 2:
                continue
            y, _ = rr.render_frames(c1, c2, segs)
            assert np.array_equal(y, full[c1 - wa:c2 - wa]), (name, c1, c2)
        if name == "pitch":
            assert sum(b - a for a, b in cuts) > 1.0 * SR      # 14 音の隙間ごとに区切れる
        # ノートの中で切ると違う（区切れる点の判定が要る）
        st, d, _m = NOTES[3]
        c = int(round((st + d / 2) * SR))
        y, _ = rr.render_frames(c, wb, segs)
        assert np.abs(y - full[c - wa:]).max() > 1e-3, name


def _long_window_project(m, tmp_path, stereo=False):
    from vocal_engine.project import Project
    path = _wav(tmp_path / "src" / "v.wav", _voice(stereo))
    p = Project.open(path, project_dir=str(tmp_path / "p"))
    p.ensure_analyzed()
    m._state["project"] = p
    m._invalidate_renderer()
    return p


@pytest.mark.parametrize("version", [1, 2])
def test_edit_cache_rerenders_only_near_the_edited_note(praat, tmp_path, version):
    from vocal_engine.project.pitch import layered_segments
    from vocal_engine.render.region import EditCache, render_region, windows_for
    m = praat
    p = _long_window_project(m, tmp_path, stereo=True)
    p.render_version = version
    notes = [n.id for n in p.take_notes]
    assert len(notes) >= 12
    for k in range(0, len(notes), 2):                # 1 つおきに少し: 全部が 1 つの長い窓
        assert m.shift_pitch(30.0, note_id=notes[k], author="human")["ok"]
    cache = EditCache(p, channels="all")
    cache.prepare()
    cache.update()
    wins = windows_for(p, layered_segments(p))
    assert len(wins) == 1 and wins[0][1] - wins[0][0] > 8.0
    start = cache.pcm.copy()
    mid = notes[len(notes) // 2]
    steps = [
        ("pitch", lambda: m.shift_pitch(150.0, note_id=mid, author="human")),
        ("undo", lambda: m.undo()),
        ("redo", lambda: m.redo()),
        ("curve", lambda: m.set_pitch_curve([[0.0, 0.0], [0.15, 80.0], [0.4, -60.0]], note_id=notes[3],
                                            author="human")),
        ("stretch", lambda: m.stretch(1.2, note_id=notes[5], author="human")),
        ("move", lambda: m.move_note(30.0, note_id=notes[8], author="human")),
        ("mute", lambda: m.mute_notes(note_ids=[notes[10]], author="human")),
        ("fade", lambda: m.set_fade([notes[2]], fade_in_sec=0.1, author="human")),
    ]
    for name, f in steps:
        assert f()["ok"], name
        u = cache.update()
        full, _ = render_region(p, renderer=cache.rr, channels="all")
        assert np.array_equal(cache.pcm, full), name
        assert 0 < u["rendered_sec"] < wins[0][1] - wins[0][0], name
        if name in ("pitch", "undo", "redo"):
            assert u["rendered_sec"] < 1.0 and u["dirty_sec"] < 1.0, (name, u["rendered_sec"])
    for _ in range(len(steps) - 2):                  # 全部取り消す（pitch は undo → redo で 1 つ）
        assert m.undo()["ok"]
        u = cache.update()
        full, _ = render_region(p, renderer=cache.rr, channels="all")
        assert np.array_equal(cache.pcm, full)
    assert np.array_equal(cache.pcm, start)          # 元の PCM に戻る


def _audition_parts_match(a, ara_id, buf):
    """試聴に残す PCM（前のファイルの切れ端を含む）が、プラグインのキャッシュの今の音と同じ。"""
    st = a._render[ara_id]
    pcm = st["audition_pcm"]
    ch = pcm["channels"]
    seen = 0
    for part in a._pcm_parts(pcm):
        raw = np.fromfile(part["path"], dtype="<f4")
        for w in part["windows"]:
            o = w["byte_offset"] // 4
            got = raw[o:o + w["frames"] * ch].reshape(-1, ch)
            assert np.array_equal(got, buf[w["start_frame"]:w["start_frame"] + w["frames"]]), w
            seen += w["frames"]
    return seen


def test_ara_render_dirty_rerenders_only_near_the_edited_note(ara, tmp_path):  # noqa: F811
    m, a = ara
    src = _wav(tmp_path / "src" / "v.wav", _voice(stereo=True))
    _open(a)
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)
    for k in range(0, len(notes), 2):
        _ok(m.shift_pitch(30, note_id=notes[k]))
    c = Cache(src)
    r = c.sync(a, "mod-A")
    assert r["reset"] and len(r["windows"]) == 1 and r["windows"][0]["frames"] > 8 * SR
    _same(c, a, "mod-A")
    start = c.buf.copy()
    mid = notes[len(notes) // 2]
    _ok(m.shift_pitch(150, note_id=mid))
    r = c.sync(a, "mod-A")
    assert not r["reset"] and 0 < sum(k for _, k in r["restore"]) < 1.0 * SR
    assert 0 < sum(w["frames"] for w in r["windows"]) < 1.0 * SR
    _same(c, a, "mod-A")
    assert _audition_parts_match(a, "mod-A", c.buf) > 8 * SR     # 前の長い窓の頭と尻が残る
    _ok(m.undo())
    r = c.sync(a, "mod-A")
    assert sum(k for _, k in r["restore"]) < 1.0 * SR
    assert np.array_equal(c.buf, start)                          # 取り消すと元の PCM
    _audition_parts_match(a, "mod-A", c.buf)
    # 時間を動かす編集・曲線・ミュートも、小分け（max_sec）の途中に編集が入っても全体と同じ
    for f in (lambda: m.stretch(1.2, note_id=notes[5]),
              lambda: m.set_pitch_curve([[0.0, 0.0], [0.15, 80.0], [0.4, -60.0]], note_id=notes[3]),
              lambda: m.move_note(-30.0, note_id=notes[9]),
              lambda: m.mute_notes(note_ids=[notes[11]])):
        _ok(f())
        r = _ok(a.ara_render_dirty("mod-A", since=c.rev, max_sec=0.05))
        c.apply(r)
    _ok(m.shift_pitch(-90, note_id=notes[1]))
    c.sync(a, "mod-A", max_sec=0.05)
    _same(c, a, "mod-A")
    _audition_parts_match(a, "mod-A", c.buf)


@needs_material("S3_M1")
@needs_model
def test_material_one_note_in_a_long_window(tmp_path, monkeypatch):
    """素材（S3_M1）: ノートの 1 つおきにピッチを当てた長い窓の中で 1 音を直す。"""
    import materials as MT
    from vocal_engine import mcp_server as m
    from vocal_engine.project import Project
    from vocal_engine.project.pitch import layered_segments
    from vocal_engine.render.region import EditCache, render_region, windows_for
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))
    p = Project.open(MT.clip("S3_M1"), project_dir=str(tmp_path / "p"))
    p.ensure_analyzed()
    m._state["project"] = p
    m._invalidate_renderer()
    try:
        notes = list(p.take_notes)
        for k in range(0, len(notes), 2):
            assert m.shift_pitch(30.0, note_id=notes[k].id, author="human")["ok"]
        cache = EditCache(p, channels="all")
        cache.prepare()
        cache.update()
        wins = windows_for(p, layered_segments(p))
        w = max(wins, key=lambda w: w[1] - w[0])
        assert 7.0 < w[1] - w[0]
        inside = [n for n in notes if n.start_sec > w[0] + 1.0 and n.end_sec < w[1] - 1.0]
        n = inside[len(inside) // 2]
        start = cache.pcm.copy()
        for name, f in (("pitch", lambda: m.shift_pitch(80.0, note_id=n.id, author="human")),
                        ("curve", lambda: m.set_pitch_curve([[0.0, 0.0], [0.1, 60.0], [0.2, -40.0]],
                                                            note_id=n.id, author="human")),
                        ("stretch", lambda: m.stretch(1.1, note_id=n.id, author="human"))):
            assert f()["ok"], name
            u = cache.update()
            full, _ = render_region(p, renderer=cache.rr, channels="all")
            assert np.array_equal(cache.pcm, full), name
            assert u["rendered_sec"] < 2.0, (name, u["rendered_sec"])
        for _ in range(3):
            assert m.undo()["ok"]
            cache.update()
        assert np.array_equal(cache.pcm, start)
    finally:
        m._state["project"] = None
        m._invalidate_renderer()
