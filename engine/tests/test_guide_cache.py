"""Guide results survive changes of the active guide (#63)."""
import os

import numpy as np
import soundfile as sf

from vocal_engine.analysis.align import Alignment
from vocal_engine.analysis.f0 import F0Result
from vocal_engine.project import Project


def _audio(tmp_path, name, freq):
    path = tmp_path / (name + ".wav")
    sr = 16000
    t = np.arange(sr * 2) / sr
    sf.write(path, 0.3 * np.sin(2 * np.pi * freq * t), sr)
    return str(path)


def _fake_analysis(monkeypatch):
    from vocal_engine.project import store, align_helper
    from vocal_engine.analysis import onsets

    calls = {"f0": 0, "dtw": 0, "onsets": 0}

    def estimate(x, sr, estimator="rmvpe", sweep=False):
        calls["f0"] += 1
        n = int(np.floor(len(x) / sr / 0.01)) + 1
        freq = np.fft.rfftfreq(sr, 1 / sr)[np.argmax(np.abs(np.fft.rfft(x[:sr])))]
        return F0Result(np.full(n, freq), np.ones(n), np.ones(n, dtype=bool),
                        np.full(n, -20.0), sr=sr, estimator=estimator,
                        meta={"sweep": sweep, "threshold": 0.03,
                              "vuv_rule": "%s f0>0 AND rms > -55.0 dBFS" % estimator})

    def align(project, method):
        calls["dtw"] += 1
        guide_end = 1.0 + project.guide_f0.f0[0] / 1000
        return Alignment.from_seconds(np.array([0.0, 1.0]), np.array([0.0, guide_end]),
                                      method=method)

    def detect(x, sr):
        calls["onsets"] += 1
        return np.array([0.25, 0.75])

    monkeypatch.setattr(store, "estimate_f0", estimate)
    monkeypatch.setattr(align_helper, "compute_alignment", align)
    monkeypatch.setattr(onsets, "detect", detect)
    return calls


def test_a_b_a_and_detach_reattach_reuse_all_guide_results(tmp_path, monkeypatch):
    calls = _fake_analysis(monkeypatch)
    take = _audio(tmp_path, "take", 220)
    a = _audio(tmp_path, "a", 330)
    b = _audio(tmp_path, "b", 440)
    directory = str(tmp_path / "project")

    alignments = []
    f0_values = []
    for guide in (a, b, a):
        p = Project.open(take, guide, project_dir=directory)
        p.analyze(auto_lyrics=False)
        alignments.append(p.alignment.to_json())
        f0_values.append(p.guide_f0.f0[0])
    assert calls == {"f0": 3, "dtw": 2, "onsets": 3}
    assert f0_values[0] != f0_values[1] and f0_values[0] == f0_values[2]
    assert alignments[0] != alignments[1] and alignments[0] == alignments[2]
    assert os.path.exists(os.path.join(directory, "cache", "alignment.json"))
    for kind in ("analysis", "alignment"):
        assert len(os.listdir(os.path.join(directory, "cache", "guide", kind))) == 2

    p.clear_guide()
    assert not os.path.exists(os.path.join(directory, "cache", "alignment.json"))
    p = Project.open(take, a, project_dir=directory)
    p.analyze(auto_lyrics=False)
    assert calls == {"f0": 3, "dtw": 2, "onsets": 3}


def test_failed_dtw_is_retried_and_never_saved(tmp_path, monkeypatch):
    calls = _fake_analysis(monkeypatch)
    from vocal_engine.project import align_helper
    take = _audio(tmp_path, "take", 220)
    guide = _audio(tmp_path, "guide", 330)
    directory = str(tmp_path / "project")

    def fail(project, method):
        calls["dtw"] += 1
        raise ValueError("failed")

    monkeypatch.setattr(align_helper, "compute_alignment", fail)
    for _ in range(2):
        p = Project.open(take, guide, project_dir=directory)
        p.analyze(auto_lyrics=False)
        assert p.alignment.info["stage"] == "fallback"
        assert not os.path.exists(os.path.join(directory, "cache", "alignment.json"))
        assert not os.path.exists(os.path.join(p._guide_cache_path, "alignment.json"))
    assert calls["dtw"] == 2


def test_full_length_guide_uses_its_tracks_take_f0(tmp_path, monkeypatch):
    calls = _fake_analysis(monkeypatch)
    guide = _audio(tmp_path, "guide", 330)
    take = _audio(tmp_path, "take", 220)
    guide_project = Project.open(guide, project_dir=str(tmp_path / "guide-project"))
    guide_project.analyze(auto_lyrics=False)
    assert calls["f0"] == 1

    p = Project.open(take, guide, project_dir=str(tmp_path / "take-project"))
    p.guide_take_cache_path = os.path.join(guide_project.dir, "cache", "take-analysis.json")
    p.analyze(auto_lyrics=False)
    assert calls["f0"] == 2  # take only; guide came from guide-project
    np.testing.assert_array_equal(p.guide_f0.f0, guide_project.take_f0.f0)


def test_shifted_guide_is_reestimated(tmp_path, monkeypatch):
    from vocal_engine import media
    calls = _fake_analysis(monkeypatch)
    guide = _audio(tmp_path, "guide", 330)
    take = _audio(tmp_path, "take", 220)
    guide_project = Project.open(guide, project_dir=str(tmp_path / "guide-project"))
    guide_project.analyze(auto_lyrics=False)

    clip = media.Clip(guide, offset_frames=160, length_frames=16000)
    p = Project.open(take, clip, project_dir=str(tmp_path / "take-project"))
    p.guide_take_cache_path = os.path.join(guide_project.dir, "cache", "take-analysis.json")
    p.analyze(auto_lyrics=False)
    assert calls["f0"] == 3  # guide clip gets its own estimate


def test_set_lyrics_then_analyze_take_keeps_guide_f0_and_dtw(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m

    calls = _fake_analysis(monkeypatch)
    monkeypatch.setattr(Project, "analyze_phonemes", lambda *args, **kwargs: None)
    take = _audio(tmp_path, "take", 220)
    guide = _audio(tmp_path, "guide", 330)
    p = Project.open(take, guide, project_dir=str(tmp_path / "project"))
    p.analysis["auto_lyrics"] = {"attempted": True}
    monkeypatch.setitem(m._state, "project", p)
    monkeypatch.setitem(m._state, "session", None)
    monkeypatch.setitem(m._state, "track", None)

    first = m.analyze_take(background=False)
    assert first.get("ok") is not False, first
    before = dict(calls)
    guide_dir = p._guide_cache_path
    alignment_dir = p._alignment_cache_dir()
    changed = m.set_lyrics("あ", start_sec=0.2, end_sec=0.8)
    assert changed.get("ok") is not False, changed
    again = m.analyze_take(background=False)
    assert again.get("ok") is not False, again

    assert calls == before
    assert p._guide_cache_path == guide_dir
    assert p._alignment_cache_dir() == alignment_dir
    for kind in ("analysis", "alignment"):
        assert len(os.listdir(os.path.join(p.dir, "cache", "guide", kind))) == 1
