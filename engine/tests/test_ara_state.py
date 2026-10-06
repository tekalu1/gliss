# -*- coding: utf-8 -*-
"""DAW（ARA）の保存の状態の署名（`ara_revs` の states・guides）と描画の版の回帰（合成の音だけ）。

プラグインは、ホストに知らせた後に保存の状態（アーカイブに入るもの）が変わったら、音が変わらなくてもホストに
「保存するものが変わった」と知らせる。そのため、利用者の変更（編集・歌詞・方式の明示・ガイド・取り消し）では署名が変わり、
解析だけ（曲を開いて解析しただけ・自動推定の歌詞）では変わらないことを見る。
"""
import copy

from test_ara_tools import _add, _ok, _open, _wav
from test_f0_switch_and_curve import _legato, _notes, eng  # noqa: F401


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
    r = _ok(a.ara_restore("mod-2", arc))
    assert r["state"] == _state(a) and r["rev"] == _ok(a.ara_revs())["revs"]["mod-2"]
    r2 = _ok(a.ara_set_modification("mod-2", m._state["session"].find_ara("mod-2")["path"]))
    assert r2["state"] == _state(a)
