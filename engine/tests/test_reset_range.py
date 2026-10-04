# -*- coding: utf-8 -*-
"""`reset_to_original(start_sec, end_sec)` — 範囲の編集を全部外して原音に戻す。

範囲で戻したら、範囲の中は**書き出した音が元のファイルとサンプル一致**になること。
今のノートの切れ目と合わないタイミングの編集（解析の方式・版が変わった後の古い編集、
音素単位の伸縮）が残っていても、補う伸縮を足して組み直すのではなく、外して元に戻す。
"""
import shutil

import numpy as np
import pytest
import soundfile as sf

from conftest import GUIDE, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


@pytest.fixture
def plain(tmp_path):
    from vocal_engine import mcp_server as S
    from vocal_engine.project import Project
    p = Project.open(TAKE, GUIDE, project_dir=str(tmp_path / "plain"))
    p.analyze()
    S._state["project"] = p
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)


def _diff_spans(p, tmp_path):
    """書き出しと元で違うサンプルの [最初, 最後]（秒）。一致なら None。"""
    from vocal_engine.render.export import export_wav
    out = str(tmp_path / "out.wav")
    export_wav(p, path=out)
    a, sr = sf.read(TAKE, dtype="int32", always_2d=True)
    b, _ = sf.read(out, dtype="int32", always_2d=True)
    assert a.shape == b.shape
    d = np.flatnonzero(np.any(a != b, axis=1))
    return None if not len(d) else (d[0] / sr, d[-1] / sr)


def _add(p, kind, t0, t1, params):
    from vocal_engine.project.model import Target
    return p.apply_edits([{"kind": kind, "target": Target.range(t0, t1), "params": params}],
                         author="human")


def _inner(p):
    from vocal_engine.project import timing as TM
    ns = TM.pitched_notes(p)
    return ns[2:-2]


def test_range_reset_removes_misaligned_timing_edits(plain, tmp_path):
    """ノートの切れ目と合わない伸縮・切り取り・無音（古い解析で作った編集）も外れて、範囲は原音に戻る。"""
    from vocal_engine import mcp_server as S
    ns = _inner(plain)
    a, b = ns[0].start_sec, ns[-1].end_sec
    m = 0.5 * (a + b)
    # 長さを保つ組（後ろをずらさない）: 伸ばした分を切り取り・縮めた分を無音で返す。境目はノートと無関係の時刻
    _add(plain, "stretch", m - 0.31, m - 0.13, {"ratio": 1.25})
    _add(plain, "crop", m - 0.13, m - 0.085, {})
    _add(plain, "stretch", m + 0.017, m + 0.2, {"ratio": 0.8})
    _add(plain, "silence", m + 0.2, m + 0.2, {"sec": 0.0366})
    S.set_pitch_curve(points=[[ns[1].start_sec + 0.02, (ns[1].pitch_midi or 60) + 0.5],
                              [ns[1].end_sec - 0.02, (ns[1].pitch_midi or 60) + 0.5]], mode="draw")
    S.shift_pitch(40.0, note_id=ns[-1].id)
    assert _diff_spans(plain, tmp_path) is not None
    r = S.reset_to_original(start_sec=a - 0.02, end_sec=b + 0.02)
    assert r["ok"] and r["changeset"], r
    assert r["added"] == 0, "補う伸縮を足さない: %s" % r
    assert not [e for e in plain.edits if e.kind in ("stretch", "crop", "silence", "pitch_draw",
                                                       "pitch_shift")], [e.kind for e in plain.edits]
    src, out = plain.time_map()
    assert float(np.max(np.abs(np.asarray(out) - np.asarray(src)))) < 1e-6
    assert _diff_spans(plain, tmp_path) is None


def test_range_reset_keeps_edits_outside(plain, tmp_path):
    """範囲の外の編集（ピッチ・タイミング）は残り、範囲の中だけが原音に戻る。"""
    from vocal_engine import mcp_server as S
    from vocal_engine.project import timing as TM
    ns = TM.pitched_notes(plain)
    first, inside = ns[0], ns[len(ns) // 2]
    S.shift_pitch(-50.0, note_id=first.id)
    S.stretch(0.85, note_id=inside.id)
    S.shift_pitch(70.0, note_id=inside.id)
    S.move_note(15.0, note_id=inside.id)
    a, b = inside.start_sec - 0.01, inside.end_sec + 0.01
    r = S.reset_to_original(start_sec=a, end_sec=b)
    assert r["ok"] and r["changeset"], r
    lo, hi = r["timing_span_sec"] or (a, b)
    kept = [e for e in plain.edits if e.kind == "pitch_shift"]
    assert [e.target.note_id for e in kept] == [first.id]
    d = _diff_spans(plain, tmp_path)
    assert d is not None and d[1] < min(lo, a), "範囲の中は原音（違うのは範囲の外の編集だけ）: %s" % (d,)


def test_range_reset_reports_widened_timing_span(plain):
    """範囲の端をまたぐタイミングの組は、時間の対応が元どおりになる所まで広げて外し、その範囲を返す。"""
    from vocal_engine import mcp_server as S
    ns = _inner(plain)
    i = len(ns) // 2
    n, nxt = ns[i], ns[i + 1]
    S.stretch(1.15, note_id=n.id)                      # 尻を動かす = 接続された次のノートも伸び縮み
    assert plain.edits
    r = S.reset_to_original(start_sec=nxt.end_sec - 0.002, end_sec=nxt.end_sec - 0.001)
    assert r["ok"]
    lo, hi = r["timing_span_sec"]
    assert lo <= n.end_sec <= hi
    src, out = plain.time_map()
    assert float(np.max(np.abs(np.asarray(out) - np.asarray(src)))) < 1e-6


def test_whole_track_reset(plain, tmp_path):
    from vocal_engine import mcp_server as S
    from vocal_engine.project import timing as TM
    ns = TM.pitched_notes(plain)
    S.correct_to_guide(pitch_strength=1.0, timing_strength=1.0)
    S.split_note(sec=0.5 * (ns[1].start_sec + ns[1].end_sec), note_id=ns[1].id)
    S.mute_notes(note_ids=[ns[0].id])
    r = S.reset_to_original(whole_track=True)
    assert r["ok"] and r["changeset"], r
    assert {e.kind for e in plain.edits} <= {"split"}, "ノートの切れ目（分割）だけ残る"
    assert _diff_spans(plain, tmp_path) is None
    S.undo()
    assert any(e.kind != "split" for e in plain.edits)
