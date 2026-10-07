# -*- coding: utf-8 -*-
"""確定済みのタイミングの編集（stretch / crop / silence）は、骨組み（structure）や接続の規則を変えても、同じ時間の対応・同じ音になる。

確定した編集は具体的な数（範囲・比・秒）だけを持ち、接続の上書き（`connection`）や骨組みの規則は読まない。beta.11 のコードで
同じ編集を入れて取った時間の対応（`GOLDEN`）と、今のコードの結果が同じことを確かめる。`connection` は今後の編集の動き方を決めるだけで、
確定済みの音は変えない（`project/timing.py` の冒頭）。素材なし（合成の音）。
"""
import numpy as np
import pytest
import soundfile as sf

SR = 22050
# beta.11 のコードで取った時間の対応（編集の前の秒 → 後の秒）
GOLDEN_SRC = [0.0, 0.1, 0.39, 0.45, 0.5, 0.6, 0.6, 0.68, 1.0, 2.0]
GOLDEN_OUT = [0.0, 0.1, 0.448, 0.508, 0.508, 0.608, 0.638, 0.718, 1.006, 2.006]


def _specs():
    from vocal_engine.project.model import Target
    return [
        {"kind": "stretch", "target": Target.range(0.10, 0.39), "params": {"ratio": 1.2}},
        {"kind": "crop", "target": Target.range(0.45, 0.50), "params": {}},
        {"kind": "silence", "target": Target.range(0.60, 0.60), "params": {"sec": 0.03}},
        {"kind": "stretch", "target": Target.range(0.68, 1.00), "params": {"ratio": 0.9}},
    ]


@pytest.fixture
def project(tmp_path):
    from vocal_engine.analysis import f0 as F
    from vocal_engine.project import Project
    F.set_preferred_estimator("praat")
    t = np.arange(int(2.0 * SR)) / SR
    path = str(tmp_path / "v.wav")
    sf.write(path, (0.2 * np.sin(2 * np.pi * 220 * t)).astype("float32"), SR)
    p = Project.open(path, None, project_dir=str(tmp_path / "p"))
    p.analyze(auto_lyrics=False)
    for spec in _specs():
        p.apply_edits([spec], author="human")
    yield p, path, str(tmp_path / "p")
    F.set_preferred_estimator(None)


def _map(p):
    src, out = p.time_map()
    return [round(float(v), 9) for v in src], [round(float(v), 9) for v in out]


def test_committed_edits_give_the_same_time_map_as_before_the_rule_change(project):
    p, _path, _dir = project
    src, out = _map(p)
    assert src == pytest.approx(GOLDEN_SRC, abs=1e-9) and out == pytest.approx(GOLDEN_OUT, abs=1e-9)


def test_reopening_the_saved_project_replays_the_same_time_map(project):
    from vocal_engine.project import Project
    p, path, pdir = project
    q = Project.open(path, None, project_dir=pdir)               # project.json の編集の列から作り直す
    assert _map(q) == _map(p)
    assert _map(q)[1] == pytest.approx(GOLDEN_OUT, abs=1e-9)


def test_connection_overrides_do_not_change_committed_audio(project):
    """接続の上書きは今後の編集の動き方だけを決める（確定済みの時間の対応・音は変えない）。"""
    from vocal_engine.project.model import Target
    from vocal_engine.render.region import RegionRenderer, render_region
    p, _path, _dir = project
    before_map = _map(p)
    rr = RegionRenderer.for_project(p, backend="praat", channels="mono")
    before, _ = render_region(p, 0.0, 1.4, renderer=rr)
    ids = [n.id for n in p.take_notes][:2] or ["n00", "n01"]
    p.apply_edits([{"kind": "connection", "target": Target.range(0.39, 0.45),
                    "params": {"a": ids[0], "b": ids[-1], "connected": False}},
                   {"kind": "connection", "target": Target.range(0.50, 0.60),
                    "params": {"a": "x1", "b": "x2", "connected": True}}], author="human")
    assert _map(p) == before_map
    after, _ = render_region(p, 0.0, 1.4, renderer=rr)
    assert np.array_equal(before, after)
