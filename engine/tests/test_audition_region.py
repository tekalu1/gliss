# -*- coding: utf-8 -*-
"""ピッチをドラッグしている間の試聴（cents ≠ 0）は、曲全体ではなく要求範囲の近くにだけ層を当てる
（`render/region.py: audition_segments_near`・`project/pitch.py: apply_layers(region=)`）。音は省かないときとビット一致。
素材なし（合成の歌声。重みの要らない Praat）。
"""
import os

import numpy as np
import pytest
import soundfile as sf

SR = 22050


def _voice(n_notes):
    y = np.zeros(int(SR * (n_notes * 0.8 + 1.5)))
    for i in range(n_notes):
        st, d, midi = 0.5 + i * 0.8, 0.55, 55 + (i * 7) % 14
        ia, ib = int(st * SR), int((st + d) * SR)
        t = np.arange(ib - ia) / SR
        f0 = 440 * 2 ** ((midi - 69) / 12)
        vib = 30 * np.sin(2 * np.pi * 5.5 * t) * np.clip((t - 0.12) / 0.1, 0, 1)
        ph = 2 * np.pi * np.cumsum(f0 * 2 ** (vib / 1200)) / SR
        s = sum((0.6 ** k) * np.sin((k + 1) * ph) for k in range(6))
        y[ia:ib] += 0.25 * s * np.minimum(1, np.minimum(t / 0.03, (d - t) / 0.04))
    return y


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    from vocal_engine import mcp_server as m
    from vocal_engine.analysis import f0 as F
    from vocal_engine.project import Project
    F.set_preferred_estimator("praat")
    base = tmp_path_factory.mktemp("region")
    path = str(base / "v.wav")
    sf.write(path, _voice(60).astype("float32"), SR)
    p = Project.open(path, None, project_dir=str(base / "p"))
    p.analyze(auto_lyrics=False)
    notes = [n for n in p.take_notes if n.kind == "note"]
    assert len(notes) >= 50
    from vocal_engine.project.model import Target
    for i, n in enumerate(notes[:45]):                       # 隣り合う編集がつながって、長い窓（5 秒を超える）になる
        p.apply_edits([{"kind": "pitch_shift", "target": Target.note(n.id), "params": {"cents": 30 + (i % 4) * 25}}], author="human")
    for i, n in enumerate(notes[5:40]):
        a, b = n.start_sec + 0.08, n.end_sec - 0.05
        pts = [[a + (b - a) * k / 29, 69 + 12 * np.log2(n.pitch_hz / 440) + 0.3 * np.sin(k / 3.0 + i)] for k in range(30)]
        from vocal_engine.project import pitch as PI
        rm, specs, _info = PI.draw_specs(p, pts)
        p.apply_changes(rm, specs, author="human", label="draw")
    yield p, notes
    F.set_preferred_estimator(None)


@pytest.mark.parametrize("cents", [100.0, -250.0, 37.5])
def test_region_limited_layers_are_bit_identical(project, cents):
    from vocal_engine.render.region import RegionRenderer, audition
    from vocal_engine.mcp_ara import audition_region
    p, notes = project
    rr = RegionRenderer.for_project(p, backend="praat", channels="mono", audition_local=True)
    for n in notes[2:56:4]:                                   # 編集のかたまりの中・端・外
        region = audition_region(None, p, n.start_sec, n.end_sec)
        y_full, _ = audition(p, n.id, cents, renderer=rr)
        y_near, info = audition(p, n.id, cents, renderer=rr, region=region)
        assert np.array_equal(y_full, y_near), n.id


def test_a_region_that_is_too_small_falls_back_to_the_whole(project):
    from vocal_engine.render import region as R
    p, notes = project
    n = notes[20]
    calls = []
    real = R.audition_segments

    def spy(project_, note_id, cents=0.0, region=None):
        calls.append(region)
        return real(project_, note_id, cents, region=region)

    R.audition_segments, old = spy, real
    try:
        segs = R.audition_segments_near(p, n.id, 100.0, n.start_sec, n.end_sec, (n.start_sec - 0.4, n.end_sec + 0.4))
    finally:
        R.audition_segments = old
    assert calls[0] is not None and calls[-1] is None         # 範囲の端に窓が触れた: 省かずに全体で作り直した
    full = R.audition_segments(p, n.id, 100.0)
    assert [(s.start_sec, s.end_sec, s.cents, s.curve_points) for s in segs] == \
           [(s.start_sec, s.end_sec, s.cents, s.curve_points) for s in full]


def test_region_limited_layers_do_less_work(project):
    """範囲の外の Segment には層を当てない（ずらし量の点列を作らない）。"""
    from vocal_engine.render.region import audition_segments
    p, notes = project
    n = notes[30]
    near = audition_segments(p, n.id, 100.0, region=(n.start_sec - 7, n.end_sec + 7))
    full = audition_segments(p, n.id, 100.0)
    layered = lambda ss: sum(1 for s in ss if getattr(s, "_layer", False))
    assert 0 < layered(near) < layered(full)
