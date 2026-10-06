# -*- coding: utf-8 -*-
"""描画の版（`Project.render_version`）の回帰（合成の音だけ）。

0.1.0-beta.7 の描画の版 2 は、ピッチ曲線を重ねたときのつなぎ目で隣のノートのずらし量が漏れない（版 1 = beta.6 までは漏れる
ことがある）。利用者が聴いて了承した既存の曲の音を Gliss の版を上げただけで変えないため:
- 版の無いアーカイブ・project.json（beta.6 までの曲）は版 1 のまま鳴らす（beta.6 の描画とサンプル一致）
- 既存の曲に新しい編集を足しても版は変わらず、前の編集の音は変わらない
- 新しい曲・編集の無い曲の最初の編集は版 2
- 上げるのは `set_render_version` で明示したときだけ（替える前に音が変わる所を返す）
"""
import copy
import json
import os

import numpy as np

from test_ara_tools import _add, _ok, _open, _wav
from test_f0_switch_and_curve import SR, _legato, _render, _song, eng  # noqa: F401


def _leaky_edits(m, p):
    """不具合 5 の形: つながった 2 音の前に +282 セント、2 音にまたがる 0 の曲線（版 1 では前のずらしが後ろに漏れる）。"""
    from vocal_engine.project import timing as TM
    ns = TM.pitched_notes(p)
    a_, b_ = ns[1], ns[2]
    _ok(m.shift_pitch(282, note_id=a_.id))
    _ok(m.set_pitch_curve([[0.0, 0.0], [b_.end_sec - a_.start_sec + 0.1, 0.0]], mode="offset",
                          start_sec=a_.start_sec - 0.05, end_sec=b_.end_sec + 0.05))
    return b_.end_sec + 0.3                                         # これより後ろに新しい編集を足す


def _beta6_render(p, monkeypatch):
    """0.1.0-beta.6 の描画（つなぎ目で曲線の点を切らない）で書き出す。"""
    from vocal_engine.project import pitch
    with monkeypatch.context() as mp:
        mp.setattr(pitch, "_clip_curve", lambda pts, lo=None, hi=None: pts)
        version, p.render_version = p.render_version, 2
        try:
            return _render(p)
        finally:
            p.render_version = version


def _daw_song(m, a, tmp_path):
    from vocal_engine import mcp_tracks as mt
    _open(a)
    tid = _add(a, "mod-1", _wav(tmp_path / "ara-src" / "mod-1.wav", _legato()), name="テイク")["track"]["id"]
    _ok(mt.select_track(tid))
    _ok(m.analyze_take(background=False))
    return m._state["project"]


def test_beta6_archive_keeps_its_sound_until_the_user_upgrades(eng, tmp_path, monkeypatch):
    m, a, md, R = eng
    p = _daw_song(m, a, tmp_path)
    assert p.render_version == 2                                    # 新しい曲は最新の版
    tail = _leaky_edits(m, p)
    beta6 = _beta6_render(p, monkeypatch)
    fixed = _render(p)
    assert not np.array_equal(beta6, fixed)                         # 不具合 5 の出る形
    arc = copy.deepcopy(_ok(a.ara_archive(["mod-1"]))["archives"]["mod-1"]["archive"])
    assert arc["render_version"] == 2
    arc.pop("render_version")                                       # beta.6 のアーカイブ
    r = _ok(a.ara_restore("mod-1", arc))
    q = m._state["project"]
    assert q.render_version == 1 and r["render_changed"] is False
    assert np.array_equal(_render(q), beta6)                        # beta.6 の音のまま（サンプル一致）
    # 書き出し（ara_render_dirty の全体）も同じ
    d = _ok(a.ara_render_dirty("mod-1", max_sec=100.0))
    assert d["windows"] and not d["more"]
    # 上げる前に、音が変わる所を返す（替えない）
    v = _ok(m.set_render_version())
    assert v["applied"] is False and v["render_version"] == 1 and v["target"] == 2
    assert v["changes"] and v["changed_sec"] > 0 and max(c["max_cents"] for c in v["changes"]) > 100
    assert np.array_equal(_render(m._state["project"]), beta6)
    # 新しい編集を足しても版は変わらず、前の編集の音は変わらない
    before_state = _ok(a.ara_revs())["states"]["mod-1"]
    _ok(m.shift_pitch(50, start_sec=tail + 0.5, end_sec=tail + 1.0))
    q = m._state["project"]
    assert q.render_version == 1
    k = int(tail * SR)
    assert np.array_equal(_render(q)[:k], beta6[:k])
    # 明示で上げると修正後の音になる（保存の状態も変わる）
    v = _ok(m.set_render_version(apply=True))
    q = m._state["project"]
    assert v["applied"] is True and q.render_version == 2
    assert np.array_equal(_render(q)[:k], fixed[:k])
    assert _ok(a.ara_revs())["states"]["mod-1"] != before_state
    assert _ok(a.ara_archive(["mod-1"]))["archives"]["mod-1"]["archive"]["render_version"] == 2
    # 戻すのも明示で
    _ok(m.set_render_version(1, apply=True))
    assert np.array_equal(_render(m._state["project"])[:k], beta6[:k])


def test_beta6_project_file_keeps_its_sound(eng, tmp_path, monkeypatch):
    """単体版の曲（project.json に描画の版の無い beta.6 までの曲）も版 1 のまま鳴らす。"""
    from vocal_engine.project import Project
    m, a, md, R = eng
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    p = m._state["project"]
    _leaky_edits(m, p)
    beta6 = _beta6_render(p, monkeypatch)
    path = os.path.join(p.dir, "project.json")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["render_version"] == 2
    doc.pop("render_version")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    q = Project(p.dir).load()
    assert q.render_version == 1
    assert np.array_equal(_render(q), beta6)


def test_first_edit_of_an_old_song_without_edits_takes_the_latest_renderer(eng, tmp_path):
    from vocal_engine.project import Project
    m, a, md, R = eng
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    p = m._state["project"]
    p.render_version = 1                                            # beta.6 で解析だけした曲
    p.save()
    assert Project(p.dir).load().render_version == 1
    _ok(m.shift_pitch(100, start_sec=0.5, end_sec=1.0))
    assert m._state["project"].render_version == 2
    assert _ok(m.set_render_version())["changes"] == []             # もう最新


def test_lyrics_only_history_still_takes_the_latest_renderer_on_the_first_edit(eng, tmp_path):
    """歌詞だけの changeset（音を作らない）がある古い曲でも、最初の音の編集で最新の描画の版にする。"""
    m, a, md, R = eng
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    p = m._state["project"]
    p.render_version = 1                                            # beta.6 で解析だけした曲
    p.save()
    _ok(m.set_lyrics("あいうえお", start_sec=0.4, end_sec=2.2, reanalyze=False))
    q = m._state["project"]
    assert q.changesets and q.render_version == 1                   # 歌詞だけでは版を変えない（音が無い）
    _ok(m.set_pitch_curve([[0.0, 0], [0.2, 80], [0.4, 0]], start_sec=0.5, end_sec=0.9))
    assert m._state["project"].render_version == 2


def test_old_song_with_an_undone_edit_keeps_its_renderer(eng, tmp_path):
    """取り消した音の編集も履歴（やり直すと前の版の音で鳴る）: 版は変えない。"""
    m, a, md, R = eng
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    _ok(m.shift_pitch(100, start_sec=0.5, end_sec=1.0))
    p = m._state["project"]
    p.render_version = 1
    p.save()
    _ok(m.undo())
    _ok(m.shift_pitch(50, start_sec=2.6, end_sec=3.0))
    assert m._state["project"].render_version == 1
