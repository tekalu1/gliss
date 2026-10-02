# -*- coding: utf-8 -*-
"""つかんだノートのプレビュー音（issue #27）: `render_audition`。

- cents = 0 なら render_region（モノラル）の同じ範囲と同じ中身
- cents を渡すと、shift_pitch(cents) を当ててから render_region したものと同じ中身（先取り）
- プロジェクトを書き換えない（編集・取り消しの履歴・project.json が変わらない）
- 範囲の既定はノートの範囲・毎回同じファイルに上書き・無いノートはエラー
"""
import hashlib
import os

import numpy as np
import pytest
import soundfile as sf

from conftest import GUIDE, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


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


def _pick(p):
    ns = [n for n in p.take_notes if n.kind == "note" and n.end_sec - n.start_sec > 0.25]
    return ns[len(ns) // 2]


def _read(path):
    y, sr = sf.read(path, dtype="float64", always_2d=True)
    return y.mean(axis=1), sr


def _hash(p):
    with open(os.path.join(p.dir, "project.json"), "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


def test_audition_zero_is_region_and_does_not_touch_project(tmp_path, mcp):
    m = mcp
    p = _open(m, str(tmp_path / "a0"))
    n = _pick(p)
    _ok(m.shift_pitch(cents=-50, note_id=n.id, author="human"))     # 編集のあるプロジェクトで
    h0 = _hash(p)
    edits0 = len(p.edits)
    hist0 = m._history_summary()
    r = _ok(m.render_audition(note_id=n.id))
    assert r["note_id"] == n.id and r["cents"] == 0
    assert r["path"].endswith("audition.wav")
    y, sr = _read(r["path"])
    assert sr == r["sr"]
    assert len(y) == int(round(n.end_sec * sr)) - int(round(n.start_sec * sr))   # 既定はノートの範囲
    rr = _ok(m.render_region(start_sec=n.start_sec, end_sec=n.end_sec, channels="mono",
                             path=str(tmp_path / "region.wav")))
    z, _ = _read(rr["path"])
    assert np.allclose(y, z, atol=1e-6)
    # プロジェクトは変わらない
    assert _hash(p) == h0
    assert len(p.edits) == edits0
    assert m._history_summary() == hist0


def test_audition_cents_matches_shift_pitch_then_region(tmp_path, mcp):
    m = mcp
    p = _open(m, str(tmp_path / "a1"))
    n = _pick(p)
    a, b = n.start_sec - 0.05, n.end_sec + 0.05
    r0 = _ok(m.render_audition(note_id=n.id, start_sec=a, end_sec=b))
    y0, sr = _read(r0["path"])
    r = _ok(m.render_audition(note_id=n.id, cents=200, start_sec=a, end_sec=b))
    assert r["path"] == r0["path"]                           # 毎回同じファイルに上書き
    assert r["cents"] == 200
    y, _ = _read(r["path"])
    assert len(y) == len(y0)
    assert np.max(np.abs(y - y0)) > 1e-3                     # 高さが変わった音
    assert r["rendered_windows_sec"]
    assert len(p.edits) == 0                                 # 先取りしただけ
    # 同じ量を当ててから区間を作ると同じ中身
    _ok(m.shift_pitch(cents=200, note_id=n.id, author="human"))
    rr = _ok(m.render_region(start_sec=a, end_sec=b, channels="mono",
                             path=str(tmp_path / "region.wav")))
    z, _ = _read(rr["path"])
    assert np.allclose(y, z, atol=1e-6)


def test_audition_errors(tmp_path, mcp):
    m = mcp
    _open(m, str(tmp_path / "a2"))
    r = m.render_audition(note_id="no-such-note")
    assert r["ok"] is False and "ノート" in r["error"]
