# -*- coding: utf-8 -*-
"""右クリックのメニューとキーのコマンド（issue #17）のエンジン側。

- 無音にする（mute_notes・Del）: 長さ・位置は変えず、そのノートの音だけ消える / 外は元のサンプルのまま /
  view data の muted / もう無音なら何もしない / undo・オリジナルに戻すで戻る
- 結合（Ctrl+J）で 3 つ以上: group で取り消し 1 回
- 子音｜母音の境目を元に戻す: reset_to_original(boundary_ids) で move_boundary だけ外す（view data の moved）
"""
import json
import os
import shutil

import numpy as np
import pytest
import soundfile as sf

import materials as M
from conftest import GUIDE, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]

LYRICS = M.text("C.lyrics")


@pytest.fixture
def mcp():
    from vocal_engine import mcp_server as m
    yield m
    m._state.update(project=None, session=None, track=None)
    m._invalidate_renderer()


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _open(m, d, **kw):
    _ok(m.open_project(TAKE, GUIDE, project_dir=d, **kw))
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
    assert a.shape == b.shape                            # 長さ・形式は元のまま
    return a, b, sr


def _pick(p):
    """ある程度長い音程ノート（中ほど）。"""
    ns = [n for n in p.take_notes if n.kind == "note" and n.end_sec - n.start_sec > 0.2]
    return ns[len(ns) // 2]


# ================================================================ 無音にする
def test_mute_silences_only_the_note(tmp_path, mcp):
    m = mcp
    p = _open(m, str(tmp_path / "m1"))
    n = _pick(p)
    r = _ok(m.mute_notes(note_ids=[n.id], author="human"))
    assert r["note_ids"] == [n.id] and r["history"]["undo"]["label"] == "無音にする"
    a, b, sr = _export(p, tmp_path / "out.wav")
    i0, i1 = int((n.start_sec + 0.012) * sr), int((n.end_sec - 0.012) * sr)
    assert np.max(np.abs(b[i0:i1])) < 1e-6                 # ノートの中は無音
    assert np.max(np.abs(a[i0:i1])) > 1e-3                 # （元は鳴っている）
    j0, j1 = int((n.start_sec - 0.012) * sr), int((n.end_sec + 0.012) * sr)
    diff = np.flatnonzero(np.any(np.abs(a - b) > 1e-9, axis=1))
    assert diff.min() >= j0 - 1 and diff.max() <= j1 + 1   # 外は元のサンプルのまま（位置も変わらない）
    vd = _view(p)
    by = {x["id"]: x for x in vd["notes"]}
    assert by[n.id]["muted"] is True
    assert sum(1 for x in vd["notes"] if x["muted"]) == 1
    # もう無音: 何もしない（履歴も増えない）
    again = _ok(m.mute_notes(note_ids=[n.id], author="human"))
    assert again["changeset"] is None
    # 戻す
    u = _ok(m.undo())
    assert u["undone"]["label"] == "無音にする"
    assert not any(e.kind == "mute" for e in p.edits)
    assert not _view(p)["notes"][0]["muted"]


def test_mute_keeps_pitch_edits_and_reset_brings_the_sound_back(tmp_path, mcp):
    m = mcp
    p = _open(m, str(tmp_path / "m2"))
    n = _pick(p)
    _ok(m.shift_pitch(cents=80, note_id=n.id, author="human"))
    _ok(m.mute_notes(note_ids=[n.id], author="human"))
    assert any(e.kind == "pitch_shift" for e in p.edits)     # ピッチの編集は残る
    _, b, sr = _export(p, tmp_path / "a.wav")
    i0, i1 = int((n.start_sec + 0.012) * sr), int((n.end_sec - 0.012) * sr)
    assert np.max(np.abs(b[i0:i1])) < 1e-6
    r = _ok(m.reset_to_original(note_ids=[n.id], author="human"))
    assert r["history"]["undo"]["label"] == "オリジナルに戻す"
    assert not any(e.kind in ("mute", "pitch_shift") for e in p.edits)
    a, b, _ = _export(p, tmp_path / "b.wav")
    assert np.array_equal(a, b)                              # 原音のサンプルそのもの


def test_mute_range_and_partial_reset_keeps_the_rest(tmp_path, mcp):
    m = mcp
    p = _open(m, str(tmp_path / "m3"))
    ns = [n for n in p.take_notes if n.kind == "note"]
    k = len(ns) // 2
    a, b = ns[k], ns[k + 1]
    r = _ok(m.mute_notes(start_sec=a.start_sec + 0.01, end_sec=b.end_sec - 0.01, author="human"))
    assert set(r["note_ids"]) >= {a.id, b.id}
    _ok(m.reset_to_original(note_ids=[a.id], author="human"))
    by = {x["id"]: x for x in _view(p)["notes"]}
    assert by[a.id]["muted"] is False and by[b.id]["muted"] is True


# ================================================================ 結合（Ctrl+J）
def test_merge_group_is_one_undo(tmp_path, mcp):
    m = mcp
    p = _open(m, str(tmp_path / "g1"))
    n = _pick(p)
    L = n.end_sec - n.start_sec
    r1 = _ok(m.split_note(sec=n.start_sec + L / 3, note_id=n.id, author="human"))
    r2 = _ok(m.split_note(sec=n.start_sec + 2 * L / 3, note_id=r1["right"], author="human"))
    ids = [n.id, r1["right"], r2["right"]]
    assert all(p.note(i) for i in ids)
    _ok(m.merge_notes(ids[0], ids[1], author="human", group="mg-1"))
    r = _ok(m.merge_notes(ids[0], ids[2], author="human", group="mg-1"))
    assert r["history"]["undo"]["label"] == "結合"
    assert [x.id for x in p.take_notes].count(n.id) == 1 and not any(
        x.id in ids[1:] for x in p.take_notes)
    _ok(m.undo())                                            # 1 回で 3 つに戻る
    assert all(any(x.id == i for x in p.take_notes) for i in ids)
    u = _ok(m.undo())                                        # 次は 2 回目の分割
    assert u["undone"]["label"] == "分割"


# ================================================================ 半音に合わせる（Q）
def test_shift_pitch_label_names_the_history_entry(tmp_path, mcp):
    m = mcp
    p = _open(m, str(tmp_path / "q1"))
    ns = [n for n in p.take_notes if n.kind == "note"][:2]
    for n in ns:
        r = _ok(m.shift_pitch(cents=12.5, note_id=n.id, author="human", group="q-1",
                              label="半音に合わせる"))
    assert r["history"]["undo"]["label"] == "半音に合わせる"
    _ok(m.undo())                                            # 1 回で両方戻る
    assert not p.edits


def test_apply_plan_label_names_the_history_entry(tmp_path, mcp):
    """右クリックの「つなぐ」: 端の計画を吸着まで伸ばして確定、履歴の名前は label。"""
    m = mcp
    p = _open(m, str(tmp_path / "c1"))
    n = _pick(p)
    r = _ok(m.plan_edit(op="edge", note_id=n.id, side="end", detach=True))
    _ok(m.apply_plan(plan_id=r["plan_id"], x=-0.05, author="human"))
    r = _ok(m.plan_edit(op="edge", note_id=n.id, side="end"))
    assert r["snap_x"] is not None
    a = _ok(m.apply_plan(plan_id=r["plan_id"], x=r["snap_x"], author="human", label="つなぐ"))
    assert a["snapped"] and a["history"]["undo"]["label"] == "つなぐ"


# ================================================================ 音素の境目を元に戻す
@pytest.fixture(scope="module")
def lyr_dir(tmp_path_factory):
    from vocal_engine.project import Project
    base = tmp_path_factory.mktemp("lyrc")
    p = Project.open(TAKE, GUIDE, project_dir=str(base / "seed"), lyrics=LYRICS,
                     guide_lyrics=LYRICS)
    p.analyze()
    return p.dir


@pytest.fixture
def lyr(lyr_dir, tmp_path):
    from vocal_engine.project import Project
    d = tmp_path / "lyr"
    shutil.copytree(os.path.join(lyr_dir, "cache"), str(d / "cache"))
    p = Project.open(TAKE, GUIDE, project_dir=str(d), lyrics=LYRICS, guide_lyrics=LYRICS)
    p.analyze()
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)


def test_reset_boundary_removes_only_move_boundary(lyr, mcp):
    m = mcp
    m._state["project"] = lyr
    m._invalidate_renderer()
    res = lyr.phonemes("take")
    b = res.boundaries[len(res.boundaries) // 2]         # 中ほど（両側に音素がある）
    n = _pick(lyr)
    _ok(m.shift_pitch(cents=30, note_id=n.id, author="human"))
    _ok(m.move_boundary(boundary_id=b.id, ms=15, author="human"))
    bd = {x["id"]: x for x in _view(lyr)["phonemes"]["boundaries"]}
    assert bd[b.id]["moved"] is True and sum(1 for x in bd.values() if x["moved"]) == 1
    r = _ok(m.reset_to_original(boundary_ids=[b.id], author="human"))
    assert r["removed"] == 1
    assert not any(e.kind == "move_boundary" for e in lyr.edits)
    assert any(e.kind == "pitch_shift" for e in lyr.edits)   # ピッチはそのまま
    bd = {x["id"]: x for x in _view(lyr)["phonemes"]["boundaries"]}
    assert bd[b.id]["moved"] is False
    _ok(m.undo(r["changeset"]))                               # 取り消せる（セッションなしは id で）
    assert any(e.kind == "move_boundary" for e in lyr.edits)
