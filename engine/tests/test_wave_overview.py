"""Waveform mipmap format with self-contained stereo input."""
import json

import numpy as np
import soundfile as sf

from vocal_engine import mcp_server  # noqa: F401  registers track tools first
from vocal_engine.mcp_tracks import _write_overview


def test_signed_mipmap_bounds_and_offsets(tmp_path):
    sr = 48000
    samples = 10001
    t = np.arange(samples) / sr
    x = np.stack((0.62 * np.sin(2 * np.pi * 317 * t),
                  0.48 * np.sin(2 * np.pi * 191 * t + 0.7)), axis=1).astype("float32")
    x[32, 0] = 0.0001  # A quiet nonzero extremum must survive outward rounding.
    src = tmp_path / "stereo.wav"
    sf.write(src, x, sr, subtype="FLOAT")
    meta_path = tmp_path / "overview.json"
    _write_overview(str(src), 0, len(x), sr, str(meta_path))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert (meta["version"], meta["sr"], meta["channels"]) == (2, sr, 2)
    assert meta["duration_sec"] == len(x) / sr
    assert [level["hop"] for level in meta["levels"]] == [32, 128, 512, 2048, 8192]
    raw = np.fromfile(tmp_path / meta["binary"], dtype="int8")
    assert len(raw) == sum(v["length"] * 4 for v in meta["levels"])
    for level in meta["levels"]:
        hop = level["hop"]
        assert level["length"] == (len(x) + hop - 1) // hop
        pairs = raw[level["offset"]:level["offset"] + level["length"] * 4].reshape(-1, 2, 2)
        for i, pair in enumerate(pairs):
            chunk = x[i * hop:(i + 1) * hop]
            expected = np.stack((chunk.min(axis=0), chunk.max(axis=0)), axis=1)
            assert np.all(pair[:, 0] / 127 <= expected[:, 0] + 1e-6)
            assert np.all(pair[:, 1] / 127 >= expected[:, 1] - 1e-6)
            assert np.max(np.abs(pair / 127 - expected)) <= 1 / 127 + 1e-6
    assert np.any(raw != 0)
