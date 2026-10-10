# -*- coding: utf-8 -*-
"""DAW（ARA）の保存の状態の署名（`ara_revs` の states・guides）と描画の版の回帰（合成の音だけ）。

プラグインは、ホストに知らせた後に保存の状態（アーカイブに入るもの）が変わったら、音が変わらなくてもホストに
「保存するものが変わった」と知らせる。そのため、利用者の変更（編集・歌詞・方式の明示・ガイド・取り消し）では署名が変わり、
解析だけ（曲を開いて解析しただけ・自動推定の歌詞）では変わらないことを見る。
"""
import copy

import pytest

from test_ara_tools import _add, _ok, _open, _wav
from test_f0_switch_and_curve import _legato, _notes, eng  # noqa: F401
from vocal_engine.phoneme import hubertfa as H

# 取り消し・やり直しは解析し直すので、音素の位置合わせ（HubertFA）の重みが要る
needs_hfa = pytest.mark.skipif(not H.model_found(), reason="HubertFA の重みが無い")


def _doc(m, a, tmp_path):
    """修飾 2 本（mod-1・mod-2）の DAW の文書。(トラック id 1, トラック id 2)。"""
    _open(a)
    t1 = _add(a, "mod-1", _wav(tmp_path / "ara-src" / "mod-1.wav", _legato()), name="テイク 1")["track"]["id"]
    t2 = _add(a, "mod-2", _wav(tmp_path / "ara-src" / "mod-2.wav", _legato(seed=1)), name="テイク 2")["track"]["id"]
    return t1, t2


def _select(m, tid):
    from vocal_engine import mcp_tracks as mt
    _ok(mt.select_track(tid))
    _ok(m.analyze_take(background=False))


def _state(a, ara_id="mod-2"):
    return _ok(a.ara_revs())["states"][ara_id]


def _erev(a, ara_id="mod-2"):
    return _ok(a.ara_revs())["revs"][ara_id].split(":")[1]


def test_analysis_alone_does_not_change_the_saved_state(eng, tmp_path):
    m, a, md, R = eng
    t1, t2 = _doc(m, a, tmp_path)
    before = _state(a)
    _select(m, t2)                                                    # 解析だけ（方式は明示しない）
    assert _state(a) == before
    # 自動推定の歌詞（解析が足す。手を入れていない区間）は、保存の状態にも編集の署名にも入れない
    erev = _erev(a)
    p = m._state["project"]
    p.lyrics["take"] = [{"start_sec": 0.5, "end_sec": 2.1, "text": "あいうえ", "origin": "estimated",
                         "estimate": {"reading": "あいうえ", "confidence": 0.9}}]
    p.save()
    assert _state(a) == before and _erev(a) == erev
    # 推定に手を入れた（音節を確かめた）区間は利用者の歌詞
    p.lyrics["take"][0]["confirmed_syllables"] = [0]
    p.save()
    assert _state(a) != before and _erev(a) != erev


@needs_hfa
def test_user_changes_change_the_saved_state(eng, tmp_path):
    from vocal_engine import mcp_tracks as mt
    m, a, md, R = eng
    t1, t2 = _doc(m, a, tmp_path)
    _select(m, t2)
    s0 = _state(a)
    _ok(m.set_lyrics("あいうえお", start_sec=0.4, end_sec=2.2, reanalyze=False))       # 歌詞だけ
    s1 = _state(a)
    assert s1 != s0
    _ok(m.set_f0_estimator("gliss", scope="current"))                                  # 編集の無い修飾の方式の明示
    s2 = _state(a)
    assert s2 not in (s0, s1)
    _ok(m.undo())                                                                       # 方式の取り消し
    assert _state(a) == s1
    _ok(m.redo())
    assert _state(a) == s2
    n = _notes(m)
    _ok(m.shift_pitch(100, note_id=n[1]))                                               # 編集
    s3 = _state(a)
    assert s3 != s2
    _ok(m.undo())
    assert _state(a) != s3                                                              # 取り消しの印も保存に入る
    # ガイドの指定は文書の状態（guides）
    assert _ok(a.ara_revs())["guides"] == {}
    _ok(mt.set_track_guide(t1, t2))
    revs = _ok(a.ara_revs())
    assert revs["guides"] == {"mod-1": "mod-2"} and revs["guide"] is None
    _ok(mt.set_guide_track(t2))
    assert _ok(a.ara_revs())["guide"] == "mod-2"


def test_restore_returns_the_state_it_restored(eng, tmp_path):
    m, a, md, R = eng
    t1, t2 = _doc(m, a, tmp_path)
    _select(m, t2)
    _ok(m.shift_pitch(100, start_sec=0.5, end_sec=1.2))
    _ok(m.set_lyrics("あいうえお", start_sec=0.4, end_sec=2.2, reanalyze=False))
    arc = copy.deepcopy(_ok(a.ara_archive(["mod-2"]))["archives"]["mod-2"]["archive"])
    assert arc["render_version"] == 2
    r = _ok(a.ara_restore("mod-2", arc))
    assert r["state"] == _state(a) and r["rev"] == _ok(a.ara_revs())["revs"]["mod-2"]
    assert r["render_changed"] is False
    r2 = _ok(a.ara_set_modification("mod-2", m._state["session"].find_ara("mod-2")["path"]))
    assert r2["state"] == _state(a)


def test_restore_reports_a_newer_renderer_that_this_engine_cannot_play(eng, tmp_path):
    """このエンジンより新しい描画の版で保存したピッチ曲線は、使える最新の版で鳴らすので音が変わりうる（render_changed）。
    前の版（版の無いアーカイブを含む）は、その版のまま鳴らすので変わらない（test_render_version.py）。"""
    m, a, md, R = eng
    t1, t2 = _doc(m, a, tmp_path)
    _select(m, t2)
    _ok(m.shift_pitch(100, start_sec=0.5, end_sec=1.2))
    arc = copy.deepcopy(_ok(a.ara_archive(["mod-2"]))["archives"]["mod-2"]["archive"])
    newer = dict(arc, render_version=99)
    assert _ok(a.ara_restore("mod-2", newer))["render_changed"] is False      # ピッチ曲線が無い
    n = _notes(m)
    _ok(m.set_pitch_curve([[0.0, 0], [0.2, 80], [0.4, 0]], note_id=n[2]))
    arc = copy.deepcopy(_ok(a.ara_archive(["mod-2"]))["archives"]["mod-2"]["archive"])
    assert _ok(a.ara_restore("mod-2", arc))["render_changed"] is False        # 今の版
    old = dict(arc)
    old.pop("render_version")
    assert _ok(a.ara_restore("mod-2", old))["render_changed"] is False        # 前の版はその版のまま鳴らす
    assert m._state["project"].render_version == 1
    r = _ok(a.ara_restore("mod-2", dict(arc, render_version=99)))
    assert r["render_changed"] is True and m._state["project"].render_version == 2


def test_first_analysis_after_restoring_into_a_new_work_folder_keeps_the_saved_state(eng, tmp_path):
    """新しい作業場所（別の PC・作業フォルダーを消した後）で Gliss の方式のアーカイブを戻し、最初の解析をしても、保存の状態は
    戻した時のまま（アーカイブの方式の版の印が解析で外れても変わらない。開いただけで「変更あり」にしない）。"""
    from vocal_engine import mcp_tracks as mt
    m, a, md, R = eng
    t1, t2 = _doc(m, a, tmp_path)
    _ok(mt.select_track(t2))
    _ok(m.analyze_take(estimator="gliss", background=False))
    _ok(m.shift_pitch(100, start_sec=0.5, end_sec=1.2))
    arc = copy.deepcopy(_ok(a.ara_archive(["mod-2"]))["archives"]["mod-2"]["archive"])
    assert arc["f0_estimator"] == "gliss" and arc["f0_estimator_version"]
    src = m._state["session"].find_ara("mod-2")["path"]
    _open(a, key="doc-2")                                           # 新しい作業場所
    _add(a, "mod-2", src, name="テイク 2")
    r = _ok(a.ara_restore("mod-2", arc))
    assert m._state["project"].f0_model_version == arc["f0_estimator_version"]
    tid = m._state["session"].find_ara("mod-2")["id"]
    _ok(mt.select_track(tid))
    _ok(m.analyze_take(background=False))                          # 最初の解析（版の印が外れる）
    assert m._state["project"].analysis["take"]["estimator"] == "gliss"
    assert _state(a) == r["state"]
    assert _ok(a.ara_archive(["mod-2"]))["archives"]["mod-2"]["archive"]["f0_estimator_version"] == arc["f0_estimator_version"]
