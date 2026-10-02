# -*- coding: utf-8 -*-
"""issue #32: 複数トラックでガイドが重ならない・「ガイドに合わせる」が使えない。

- 対応付け（DTW）: ほぼ無音の窓で MFCC が 0 ベクトルになっても NaN で落ちない。片側が無音の窓は取り直さない
- それでも対応付けが失敗したら、解析全体は落とさず位置のままの対応で続け、理由を返す
- 外した最初のテイクを足し直すと、前の編集（"."）が戻る
- テイクを開いて最初のテイクが足し直されたら、取り消しの履歴に入る（前の操作の Ctrl+Z でまた消えない）
- ガイドが重ならない理由を list_tracks の guide_note で返す
"""
import os
import shutil

import numpy as np
import pytest
import soundfile as sf

from conftest import GUIDE, TAKE, needs_clips, needs_model, stem


# ---------------------------------------------------------------- DTW
def test_dtw_path_zero_columns_do_not_raise():
    from vocal_engine.analysis import align as A
    rng = np.random.default_rng(0)
    m1 = rng.normal(size=(19, 40))
    m2 = rng.normal(size=(19, 30))
    # 0 ベクトルが無ければ librosa に任せたときと同じ経路
    import librosa
    _, wp = librosa.sequence.dtw(X=m1, Y=m2, metric="cosine", subseq=False, backtrack=True)
    assert np.array_equal(A._dtw_path(m1, m2), np.asarray(wp[::-1].T, dtype="float64"))
    # 片側が全部 0 ベクトル（ほぼ無音の窓）でも落ちない
    z = np.zeros((19, 30))
    with pytest.raises(Exception):
        librosa.sequence.dtw(X=m1, Y=z, metric="cosine", subseq=False, backtrack=True)
    wp = A._dtw_path(m1, z)
    assert wp.shape[0] == 2 and np.all(np.isfinite(wp))
    # 一部の列だけ 0 ベクトル
    m3 = m2.copy()
    m3[:, 5:12] = 0.0
    wp = A._dtw_path(m1, m3)
    assert wp[0, 0] == 0 and wp[1, 0] == 0 and wp[0, -1] == 39 and wp[1, -1] == 29


def _tone(sr, dur, f, amp=0.3):
    t = np.arange(int(sr * dur)) / sr
    return amp * np.sin(2 * np.pi * f * t) * (1 + 0.3 * np.sin(2 * np.pi * 5 * t))


def test_align_seconds_with_near_silent_guide_region():
    """取り直す窓に対応するガイドの区間がほぼ無音（MFCC の下限より静か。ユーザーの曲と同じ）でも通る。
    直す前はこの組で `DTW cost matrix C has NaN values` になっていた（確かめた）。"""
    from vocal_engine.analysis import align as A
    sr = 22050
    rng = np.random.default_rng(1)
    take = np.concatenate([_tone(sr, 1.0, 330), _tone(sr, 1.0, 262), rng.normal(scale=1e-3, size=sr * 6)])
    # ガイド: 頭で歌って、後ろ 6 秒は −140 dBFS 程度の雑音だけ（MFCC が全フレーム同じ値 = 0 ベクトル）
    guide = np.concatenate([_tone(sr, 1.0, 330), _tone(sr, 1.0, 262), rng.normal(scale=1e-7, size=sr * 6)])
    assert A._flat_cols(A._mfcc(guide[-sr * 2:], sr)).all()
    ranges = [(0.2, 1.8), (4.5, 5.5)]
    # max_cells を小さくして曲全体（粗 → 精の 2 段）と同じ経路を通す
    ta, ga, fr, info = A.align_seconds(take, sr, guide, sr, refine_ranges=ranges, max_cells=20_000)
    assert info["stage"] == "coarse+refine"
    assert info["refined"] == 1                  # ガイドが無音の窓は取り直さない（粗い対応のまま）
    assert np.all(np.isfinite(ta)) and np.all(np.isfinite(ga))
    assert np.all(np.diff(ga) >= 0)


# ---------------------------------------------------------------- MCP
@pytest.fixture
def mcp():
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_tracks as mt
    yield m, mt
    m._state.update(project=None, session=None, track=None)
    m._invalidate_renderer()


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _wait(m, r):
    import time
    while r.get("status") == "running":
        time.sleep(0.2)
        r = m.get_job(job_id=r["job_id"])
    return r


@needs_clips
@needs_model
def test_alignment_failure_falls_back_and_guide_still_shows(tmp_path, mcp, monkeypatch):
    """対応付けが例外を出しても analyze_take は通り、ガイドは位置のまま重なる（理由を返す・キャッシュしない）。"""
    m, mt = mcp
    from vocal_engine.project import align_helper as H
    d = str(tmp_path / "fb")
    _ok(m.open_project(TAKE, GUIDE, project_dir=d))

    def boom(*a, **kw):
        raise ValueError("DTW cost matrix C has NaN values. ")
    monkeypatch.setattr(H, "compute_alignment", boom)
    r = _ok(_wait(m, m.analyze_take()))
    assert r["alignment"]["dtw"]["stage"] == "fallback"
    assert "位置のまま" in r["guide_warning"] and "NaN" in r["guide_warning"]
    assert not os.path.exists(os.path.join(d, "cache", "alignment.json"))
    v = _ok(m.export_view_data())
    assert v["guide"] is True
    import json
    with open(v["path"], encoding="utf-8") as f:
        vd = json.load(f)
    assert vd["guide_warning"] and vd["guide_notes"]
    # 「ガイドに合わせる」の計画も作れる（ガイドが無い、にならない）
    note = [n for n in m._state["project"].take_notes if n.kind == "note"][0]
    _ok(m.plan_edit(op="guide", note_ids=[note.id]))
    # 直ったら（次の解析で）取り直してキャッシュする
    monkeypatch.undo()
    m._state["project"]._alignment = None
    r = _ok(_wait(m, m.analyze_take()))
    assert r["alignment"]["dtw"]["stage"] != "fallback" and "guide_warning" not in r
    assert os.path.exists(os.path.join(d, "cache", "alignment.json"))


@needs_clips
def test_readding_primary_take_reuses_its_edits(tmp_path, mcp):
    """外した最初のテイクを同じファイルで足し直すと、前の編集（"."）が戻る（新しい空のプロジェクトにしない）。"""
    m, mt = mcp
    d = str(tmp_path / "re")
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    t1, t2 = [t["id"] for t in r["session"]["tracks"]]
    _ok(m.shift_pitch(30.0, start_sec=0.5, end_sec=0.8, author="human"))
    _ok(mt.select_track(t2))
    _ok(mt.remove_track(t1))
    r = _ok(mt.add_track(TAKE, select=True))
    t3 = r["track"]
    tr = {t["id"]: t for t in r["session"]["tracks"]}
    assert os.path.normcase(tr[t3]["project_dir"]) == os.path.normcase(os.path.abspath(d))
    assert r["edits"] == 1
    # 前の id のまま戻る（取り消しの履歴の「入れた changeset」を引き継ぐ）: 次の Ctrl+Z は足し直しで、
    # 前のピッチの編集ではない（新しい id だと、前の編集が履歴の末尾に足し直されて先に取り消されていた）
    assert t3 == t1
    u = _ok(m.undo())
    assert u["undone"]["label"] == "トラックの追加"
    _ok(m.redo())
    assert _ok(mt.list_tracks())["history"]["undo"]["label"] == "トラックの追加"
    # 2 本目以降のトラックは今までどおり tracks/ の下（外して足し直すと同じ置き場）
    _ok(mt.remove_track(t2))
    t4 = _ok(mt.add_track(GUIDE))["track"]
    tr = {t["id"]: t for t in _ok(mt.list_tracks())["tracks"]}
    assert os.path.basename(os.path.dirname(tr[t4]["project_dir"])) == "tracks"
    # "." は 1 本だけ: もう使われていれば、同じ中身の別のファイルは tracks/ へ
    other = str(tmp_path / "copy_of_take.wav")
    shutil.copy(TAKE, other)
    _ok(mt.add_track(other))
    tr = [t for t in _ok(mt.list_tracks())["tracks"]
          if os.path.normcase(t["project_dir"] or "") == os.path.normcase(os.path.abspath(d))]
    assert len(tr) == 1 and tr[0]["id"] == t3


@needs_clips
def test_open_readds_primary_into_history(tmp_path, mcp):
    """最初のテイクを外した後にテイクを開くと、先頭に足し直す。これは取り消しの履歴に入り、
    その前の操作を Ctrl+Z しても足し直したトラックとスナップショットが食い違わない。"""
    m, mt = mcp
    d = str(tmp_path / "hist")
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    t1, t2 = [t["id"] for t in r["session"]["tracks"]]
    _ok(mt.select_track(t2))
    _ok(mt.remove_track(t1))                     # h: トラックを外す
    _ok(mt.set_guide_track(None))                # h: ガイドの指定（外す）
    r = _ok(m.open_project(TAKE, project_dir=d))  # 開き直す → 最初のテイクが戻る
    ss = r["session"]
    assert [t["name"] for t in ss["tracks"]] == [stem(TAKE), stem(GUIDE)]
    assert ss["tracks"][0]["project_dir"] == os.path.abspath(d)
    assert ss["history"]["undo"]["label"] == "最初のテイクを戻す"
    assert r["session"]["last_current"] == t2
    # 1 回目の Ctrl+Z: 足し直しを戻す（外した状態へ）
    u = _ok(m.undo())
    assert u["undone"]["label"] == "最初のテイクを戻す"
    assert [t["id"] for t in _ok(mt.list_tracks())["tracks"]] == [t2]
    # 2 回目: ガイドの指定を戻す。3 回目: 外したのを戻す → 元の t1 が 1 本だけ
    _ok(m.undo())
    _ok(m.undo())
    ids = [t["id"] for t in _ok(mt.list_tracks())["tracks"]]
    assert ids == [t1, t2]
    dirs = [t["project_dir"] for t in _ok(mt.list_tracks())["tracks"]]
    assert dirs.count(os.path.abspath(d)) == 1
    # 何も変わらない開き直しは履歴に入れない
    n = _ok(mt.list_tracks())["history"]["size"]
    _ok(m.open_project(TAKE, project_dir=d))
    assert _ok(mt.list_tracks())["history"]["size"] == n


@needs_clips
def test_guide_note_explains_missing_overlay(tmp_path, mcp):
    """ガイドが重ならない理由を list_tracks（と各ツールの session）の guide_note で返す。"""
    m, mt = mcp
    d = str(tmp_path / "why")
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    t1, t2 = [t["id"] for t in r["session"]["tracks"]]
    assert _ok(mt.list_tracks())["guide_note"] is None
    r = _ok(mt.set_guide_track(t1))              # 編集中のトラック自身をガイドに
    assert r["session"]["guide_note"] == "編集対象がガイドのトラック自身"
    r = _ok(mt.set_guide_track(None))
    assert r["session"]["guide_note"] == "ガイドが指定されていない"
    _ok(mt.set_guide_track(t2))
    r = _ok(mt.set_track(t2, offset_sec=600.0))   # 位置が重ならない
    assert "重ならない" in r["session"]["guide_note"]
