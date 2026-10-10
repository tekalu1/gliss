# -*- coding: utf-8 -*-
"""合成音声で試聴用の区間レンダリングを検証する。"""
import os
import time
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from vocal_engine.render.pipeline import Segment
from vocal_engine.render.region import RegionRenderer, render_region


class SyntheticProject:
    def __init__(self, duration=180.0, sr=16000):
        sr = int(os.environ.get("GLISS_AUDITION_SR", sr))
        self.duration_sec = duration
        self.sr = sr
        n = int(duration * sr)
        t = np.arange(n) / sr
        gate = (t % 0.6) < 0.3
        self.x = (0.15 * np.sin(2 * np.pi * 220 * t) * gate).astype("float64")
        nf = int(duration / 0.01) + 1
        f0 = np.where((np.arange(nf) * 0.01 % 0.6) < 0.3, 220.0, 0.0)
        self.take_f0 = SimpleNamespace(f0=f0, voiced=f0 > 0, hop_s=0.01,
                                       rms_db=np.where(f0 > 0, -25.0, -90.0))
        self.take = {"offset_frames": 0}

    def ensure_analyzed(self):
        return self

    def audio(self, role):
        assert role == "take"
        return self.x, self.sr

    def note(self, note_id):
        if note_id != "target":
            raise ValueError("unknown note")
        return SimpleNamespace(id=note_id, start_sec=90.05, end_sec=90.25)

    def reload_if_changed(self):
        return False

    def view_key(self):
        return "synthetic-view-rev"

    def sub(self, name):
        path = self.directory / name
        path.mkdir(exist_ok=True)
        return str(path)


@pytest.mark.parametrize("backend", ["praat", "psola"])
def test_long_audition_renders_only_requested_note(backend):
    p = SyntheticProject()
    segs = [Segment(i * 0.6, i * 0.6 + 0.3, cents=100.0) for i in range(300)]
    if os.environ.get("GLISS_AUDITION_PERF"):
        t0 = time.perf_counter()
    rr = RegionRenderer.for_project(p, backend=backend, audition_local=True)
    if os.environ.get("GLISS_AUDITION_PERF"):
        print("prepare-project", backend, time.perf_counter() - t0)
    for i in (0, 150, 299, 150):
        a = i * 0.6 + 0.05
        b = a + 0.2
        t0 = time.perf_counter()
        y, info = render_region(p, a, b, renderer=rr, segs=segs, audition_fast=True)
        if os.environ.get("GLISS_AUDITION_PERF"):
            print("render", backend, i, time.perf_counter() - t0, info["timing_sec"])
        assert len(y) == round(b * p.sr) - round(a * p.sr)
        assert len(info["rendered_windows_sec"]) == 1
        assert info["rendered_windows_sec"][0][1] - info["rendered_windows_sec"][0][0] < 6
        if i != 150 or not getattr(p, "_middle_compared", False):
            if i == 150:
                p._middle_compared = True
            ref, _ = render_region(p, a, b, renderer=rr, segs=segs)
            np.testing.assert_allclose(y, ref, rtol=0, atol=1e-12)


def test_mcp_audition_paths_are_immutable_and_identify_revision(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_ara
    from vocal_engine.render import region

    p = SyntheticProject(duration=9.0)
    p.directory = tmp_path
    p.take.update(source_id="synthetic-source", sha256="synthetic", frames=len(p.x))
    p.analysis = {}
    p.edits = []
    p.lyrics = {}
    # A short independent segment on this 9 s source.
    p.note = lambda note_id: SimpleNamespace(id=note_id, start_sec=4.2, end_sec=4.4)
    segs = [Segment(4.2, 4.4, cents=100.0)]
    monkeypatch.setattr(region, "audition_segments", lambda *_: segs)
    monkeypatch.setitem(m._state, "project", p)
    monkeypatch.setitem(m._state, "session", None)
    monkeypatch.setitem(m._state, "track", None)
    monkeypatch.setitem(m._state, "region", {})

    t0 = time.perf_counter()
    one = m.render_audition.__wrapped__("target", backend="praat")
    direct_sec = time.perf_counter() - t0
    first, _ = sf.read(one["path"], dtype="float32")
    monkeypatch.setattr(m.bridge, "is_app", lambda: True)
    t0 = time.perf_counter()
    two = m.render_audition("target", backend="praat")
    wrapped_sec = time.perf_counter() - t0
    if os.environ.get("GLISS_AUDITION_PERF"):
        print("mcp-direct", direct_sec, "mcp-locked-wrapper", wrapped_sec,
              "render", two["timing_sec"])
    still, _ = sf.read(one["path"], dtype="float32")
    assert one["path"] != two["path"]
    np.testing.assert_array_equal(first, still)
    assert one["rev"] == ":".join(mcp_ara._rev_parts(p))
    assert one["view_rev"] == "synthetic-view-rev"
    assert one["source_id"] == "synthetic-source"
    assert one["note_id"] == "target"
    assert ("praat", "mono", "audition") in m._state["region"]
    assert ("praat", "mono") not in m._state["region"]

    # 描画応答と同じ版を返し、レンダー中に版が進んだ応答はファイルを作らず破棄する。
    monkeypatch.setattr(m, "_export_view_data", lambda *_args, **_kw: {"path": "synthetic"})
    view = m.export_view_data.__wrapped__()
    assert view["view_rev"] == one["view_rev"]
    revs = iter(("before", "after"))
    p.view_key = lambda: next(revs)
    before_files = set((tmp_path / "renders").glob("audition-*.wav"))
    with pytest.raises(m.ProjectConflict):
        m.render_audition.__wrapped__("target", backend="praat")
    assert set((tmp_path / "renders").glob("audition-*.wav")) == before_files


def test_audition_file_limit_keeps_recent_results(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m

    class P:
        def sub(self, name):
            d = tmp_path / name
            d.mkdir(exist_ok=True)
            return str(d)

    monkeypatch.setattr(m, "_AUDITION_MAX_FILES", 3)
    monkeypatch.setattr(m, "_AUDITION_MAX_BYTES", 1000)
    monkeypatch.setattr(m.time, "time", lambda: 1000.0)
    directory = tmp_path / "renders"
    directory.mkdir()
    for i in range(3):
        f = directory / ("audition-%d.wav" % i)
        f.write_bytes(b"safe")
        os.utime(f, (980.0, 980.0))
    with pytest.raises(Exception, match="上限"):
        m._audition_output_path(P(), 2)
    assert len(list(directory.glob("audition-*.wav"))) == 3
    monkeypatch.setattr(m.time, "time", lambda: 1011.0)
    new = m._audition_output_path(P(), 2)
    assert not any(directory.glob("audition-*.wav"))
    assert new.endswith(".wav")


def test_audition_file_stress_keeps_all_recent_paths(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m

    class P:
        def sub(self, name):
            d = tmp_path / name
            d.mkdir(exist_ok=True)
            return str(d)

    monkeypatch.setattr(m.time, "time", lambda: 1000.0)
    directory = tmp_path / "renders"
    directory.mkdir()
    for i in range(600):  # 45 msごとの30秒相当。既返却pathは全て保持する。
        f = directory / ("audition-%d.wav" % i)
        f.write_bytes(b"pcm")
        os.utime(f, (999.0, 999.0))
    new = m._audition_output_path(P(), 44100)
    assert new not in [str(f) for f in directory.glob("audition-*.wav")]
    assert len(list(directory.glob("audition-*.wav"))) == 600
    monkeypatch.setattr(m.time, "time", lambda: 1031.0)
    m._audition_output_path(P(), 44100)
    assert not list(directory.glob("audition-*.wav"))


@pytest.mark.parametrize("at", [90.0, 178.2])
def test_long_psola_keeps_timing_draw_mute_fade_and_source_position(at):
    p = SyntheticProject()
    segs = [Segment(at, at + 0.3, curve_points=[[0.0, 100.0], [0.3, 200.0]]),
            Segment(at + 0.3, at + 0.6, ratio=1.2),
            Segment(at + 0.6, at + 0.9, gain=0.0),
            Segment(at + 0.9, at + 1.2, fade=(at + 0.9, at + 1.2, "out"))]
    local = RegionRenderer.for_project(p, backend="psola", audition_local=True)
    whole = RegionRenderer.for_project(p, backend="psola")
    a, b = at - 0.1, at + 1.3
    got, info = render_region(p, a, b, renderer=local, segs=segs)
    expected, _ = render_region(p, a, b, renderer=whole, segs=segs)
    np.testing.assert_allclose(got, expected, rtol=0, atol=1e-12)
    assert info["start_frame"] == round(a * p.sr)
    assert info["source_start_frame"] == info["start_frame"]


def test_long_audition_pitch_draw_mute_fade_matches_full_window():
    p = SyntheticProject()
    segs = [Segment(i * 0.6, i * 0.6 + 0.3, cents=100.0) for i in range(300)]
    segs[149] = Segment(149 * 0.6, 149 * 0.6 + 0.3, gain=0.0)
    segs[150] = Segment(90.0, 90.3, curve_points=[[0.0, 100.0], [0.3, 200.0]])
    segs.append(Segment(90.0, 90.3, fade=(90.0, 90.3, "in")))
    rr = RegionRenderer.for_project(p, backend="praat")
    a, b = 90.05, 90.25
    fast, info = render_region(p, a, b, renderer=rr, segs=segs, audition_fast=True)
    full, _ = render_region(p, a, b, renderer=rr, segs=segs)
    assert info["rendered_windows_sec"][0][1] - info["rendered_windows_sec"][0][0] < 6
    np.testing.assert_allclose(fast, full, rtol=0, atol=1e-12)


@pytest.mark.parametrize("backend", ["praat", "psola"])
@pytest.mark.parametrize("at", [0.05, 90.05, 179.65])
def test_one_long_pitch_segment_stays_local_and_quality_matches(backend, at):
    p = SyntheticProject()
    segs = [Segment(0.0, 180.0, cents=100.0)]
    rr = RegionRenderer.for_project(p, backend=backend, audition_local=True)
    normal = RegionRenderer.for_project(p, backend=backend)
    a, b = at, at + 0.2
    t0 = time.perf_counter()
    fast, info = render_region(p, a, b, renderer=rr, segs=segs, audition_fast=True)
    if os.environ.get("GLISS_AUDITION_PERF"):
        print("long-pitch", backend, at, time.perf_counter() - t0, info["timing_sec"])
    full, _ = render_region(p, a, b, renderer=normal, segs=segs)
    assert info["rendered_windows_sec"][0][1] - info["rendered_windows_sec"][0][0] < 1.0
    if backend == "psola":
        np.testing.assert_allclose(fast, full, rtol=0, atol=1e-12)
    else:
        # Praatは長いsegmentを途中で切ると位相が変わる。音量と補正後の周波数を照合する。
        rms = lambda y: float(np.sqrt(np.mean(y ** 2)))
        assert abs(rms(fast) - rms(full)) < 0.01
        peak = lambda y: int(np.argmax(np.abs(np.fft.rfft(y[:, 0]))))
        assert abs(peak(fast) - peak(full)) <= 1


def test_timewarp_uses_full_window_for_exact_audition_audio():
    p = SyntheticProject()
    segs = [Segment(i * 0.6, i * 0.6 + 0.3, cents=100.0) for i in range(300)]
    segs[149] = Segment(149 * 0.6, 149 * 0.6 + 0.3, ratio=1.2)
    segs[150] = Segment(90.0, 90.3, curve_points=[[0.0, 100.0], [0.3, 200.0]])
    rr = RegionRenderer.for_project(p, backend="praat")
    a, b = 90.05, 90.25
    got, info = render_region(p, a, b, renderer=rr, segs=segs, audition_fast=True)
    full, _ = render_region(p, a, b, renderer=rr, segs=segs)
    assert info["rendered_windows_sec"][0][1] - info["rendered_windows_sec"][0][0] > 170
    np.testing.assert_array_equal(got, full)


@pytest.mark.skipif(not os.environ.get("GLISS_AUDITION_PERF"), reason="明示した性能計測のみ")
def test_dense_timing_edits_expose_full_window_cost():
    p = SyntheticProject()
    segs = [Segment(i * 0.6, i * 0.6 + 0.3, cents=100.0, ratio=1.02)
            for i in range(300)]
    rr = RegionRenderer.for_project(p, backend="praat", audition_local=True)
    normal = RegionRenderer.for_project(p, backend="praat")
    for at in (90.05, 179.65):
        t0 = time.perf_counter()
        fast, info = render_region(p, at, at + 0.2, renderer=rr, segs=segs,
                                   audition_fast=True)
        elapsed = time.perf_counter() - t0
        print("dense-timing", at, elapsed, info["timing_sec"],
              info["rendered_windows_sec"])
        full, _ = render_region(p, at, at + 0.2, renderer=normal, segs=segs)
        np.testing.assert_array_equal(fast, full)
        assert info["rendered_windows_sec"][0][1] - info["rendered_windows_sec"][0][0] > 170


def test_window_pcm_cache_is_bounded_and_invalidates_edit_change():
    p = SyntheticProject(duration=4.0)
    rr = RegionRenderer.for_project(p, backend="praat")
    a, b = round(0.0 * p.sr), round(0.5 * p.sr)
    first = [Segment(0.0, 0.3, cents=100.0)]
    y0, _ = rr.render_frames(a, b, first)
    size = rr._window_pcm_bytes
    y1, _ = rr.render_frames(a, b, first)
    assert rr._window_pcm_bytes == size
    assert len(rr._window_pcm) == 1
    np.testing.assert_array_equal(y0, y1)
    changed = [Segment(0.0, 0.3, cents=200.0)]
    y2, _ = rr.render_frames(a, b, changed)
    assert len(rr._window_pcm) == 2
    assert rr._window_pcm_bytes == 2 * size
    assert np.max(np.abs(y2 - y1)) > 0.01
    key = next(reversed(rr._window_pcm))
    rr._remember_window(key, y2, {"warnings": [], "backend": "praat"})
    assert rr._window_pcm_bytes == 2 * size
    large = np.zeros((700000, 1), dtype="float64")
    for i in range(6):
        rr._remember_window(("large", i), large, {"warnings": []})
    assert len(rr._window_pcm) <= 4
    assert rr._window_pcm_bytes <= 16 * 1024 * 1024
    assert rr._window_pcm_bytes == sum(v[0].nbytes for v in rr._window_pcm.values())


def test_long_praat_pitch_tier_bulk_read_matches_point_queries(monkeypatch):
    from parselmouth.praat import call
    import parselmouth
    from vocal_engine.render import praat

    sr = 16000
    t = np.arange(sr * 12) / sr
    snd = parselmouth.Sound(0.2 * np.sin(2 * np.pi * 220 * t), sampling_frequency=sr)
    pt = call(call(snd, "To Manipulation", 0.01, 50.0, 600.0), "Extract pitch tier")
    count = int(call(pt, "Get number of points"))
    assert count > 1000
    times, values = praat._pitch_tier_points(pt, count)
    for idx in (1, count // 2, count):
        assert times[idx - 1] == float(call(pt, "Get time from index", idx))
        assert values[idx - 1] == float(call(pt, "Get value at index", idx))
    def unavailable(*_):
        raise OSError("busy")
    monkeypatch.setattr(praat, "_pitch_tier_points", unavailable)
    ctx = praat.PraatContext(snd.values[0], sr, np.full(1201, 220.0),
                             np.ones(1201, dtype=bool))
    assert ctx.error is None and len(ctx.pt_t) > 1000


def test_ara_renderer_reuse_requires_matching_source_and_analysis(tmp_path, monkeypatch):
    from collections import OrderedDict
    from vocal_engine import mcp_server as _mcp_server  # noqa: F401
    from vocal_engine import mcp_ara

    p = SimpleNamespace(dir=str(tmp_path), take={"sha256": "source-a"})
    rr = object()
    key = (mcp_ara._norm(p.dir), "source-a", "praat", "all", "analysis-a")
    monkeypatch.setattr(mcp_ara, "_renderers", OrderedDict({"mod": (key, rr)}))
    assert mcp_ara.audition_renderer("mod", p, "praat", "analysis-a") is rr
    assert mcp_ara.audition_renderer("mod", p, "psola", "analysis-a") is None
    assert mcp_ara.audition_renderer("mod", p, "praat", "analysis-b") is None
    p.take["sha256"] = "source-b"
    assert mcp_ara.audition_renderer("mod", p, "praat", "analysis-a") is None


def test_zero_cent_audition_reuses_ara_rendered_window(tmp_path, monkeypatch):
    from collections import OrderedDict
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_ara
    from vocal_engine.render import region

    p = SyntheticProject(duration=9.0)
    p.directory = tmp_path
    p.dir = str(tmp_path)
    p.take.update(source_id="source-a", sha256="source-a", sr=p.sr, frames=len(p.x))
    p.analysis = {}
    p.edits = []
    p.lyrics = {}
    p.note = lambda note_id: SimpleNamespace(id=note_id, start_sec=4.2, end_sec=4.4)
    segs = [Segment(4.2, 4.4, cents=100.0)]
    monkeypatch.setattr(region, "audition_segments", lambda *_: segs)
    rr = RegionRenderer.for_project(p, backend="praat")
    wa, wb = region.windows_for(p, segs)[0]
    rr.render_frames(round(wa * p.sr), round(wb * p.sr), segs)
    asig, _ = mcp_ara._rev_parts(p)
    key = (mcp_ara._norm(p.dir), "source-a", "praat", "all", asig)
    monkeypatch.setattr(mcp_ara, "_renderers", OrderedDict({"mod": (key, rr)}))
    monkeypatch.setattr(rr, "_get", lambda ch: pytest.fail("ARA window PCM was not reused"))
    monkeypatch.setitem(m._state, "project", p)
    monkeypatch.setitem(m._state, "session", SimpleNamespace(track=lambda track_id: {"ara_id": "mod"}))
    monkeypatch.setitem(m._state, "track", "track-a")
    monkeypatch.setitem(m._state, "region", {})
    result = m.render_audition.__wrapped__("target", backend="praat")
    assert result["ara_id"] == "mod"
    assert result["track_id"] == "track-a"
    assert result["view_rev"] == p.view_key()
    assert os.path.exists(result["path"])


def test_zero_cent_reuses_exact_ara_pcm_and_rejects_stale_source(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_ara

    p = SyntheticProject(duration=9.0)
    p.directory = tmp_path
    p.dir = str(tmp_path)
    p.take.update(source_id="source-a", sha256="source-a", sr=p.sr,
                  frames=len(p.x))
    p.analysis = {}
    p.edits = []
    p.lyrics = {}
    p.note = lambda note_id: SimpleNamespace(id=note_id, start_sec=4.2, end_sec=4.4)
    ia, ib = round(4.2 * p.sr), round(4.4 * p.sr)
    first = round(4.0 * p.sr)
    frames = round(0.8 * p.sr)
    stereo = np.column_stack((np.linspace(-0.2, 0.2, frames),
                              np.linspace(0.1, 0.3, frames))).astype("<f4")
    ara_path = tmp_path / "rendered.f32"
    ara_path.write_bytes(stereo.tobytes())
    asig, erev = mcp_ara._rev_parts(p)
    rev = "%s:%s" % (asig, erev)
    state = {"rev": rev, "asig": asig, "backend": "praat", "pending": [],
             "project_dir": mcp_ara._norm(p.dir), "audition_pcm": {
                 "path": str(ara_path), "windows": [{"start_frame": first,
                     "frames": frames, "byte_offset": 0}],
                 "channels": 2, "sr": p.sr, "source_frames": len(p.x)}}
    monkeypatch.setitem(mcp_ara._render, "mod", state)
    monkeypatch.setitem(m._state, "project", p)
    monkeypatch.setitem(m._state, "session", SimpleNamespace(track=lambda _: {"ara_id": "mod"}))
    monkeypatch.setitem(m._state, "track", "track-a")
    monkeypatch.setitem(m._state, "region", {})
    result = m.render_audition.__wrapped__("target", backend="praat")
    actual, sr = sf.read(result["path"], dtype="float32")
    expected = stereo[ia - first:ib - first].mean(axis=1).astype("float32")
    np.testing.assert_array_equal(actual, expected)
    assert sr == p.sr and result["timing_sec"]["ara_pcm_reused"]
    assert ("praat", "mono", "audition") not in m._state["region"]

    p.take["sha256"] = "source-b"
    new_asig, new_erev = mcp_ara._rev_parts(p)
    assert mcp_ara.audition_pcm("mod", p, "praat", new_asig,
                                "%s:%s" % (new_asig, new_erev), ia, ib) is None
    p.take["sha256"] = "source-a"
    assert mcp_ara.audition_pcm("mod", p, "praat", asig, rev, ia, ib) is not None
    p.analysis["take"] = {"analyzed_at": "new-analysis", "estimator": "gliss"}
    changed_asig, changed_erev = mcp_ara._rev_parts(p)
    assert mcp_ara.audition_pcm("mod", p, "praat", changed_asig,
                                "%s:%s" % (changed_asig, changed_erev), ia, ib) is None
    p.analysis.clear()
    state["audition_pcm"]["path"] = str(tmp_path / "missing.f32")
    assert mcp_ara.audition_pcm("mod", p, "praat", asig, rev, ia, ib) is None
