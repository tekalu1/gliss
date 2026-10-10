# -*- coding: utf-8 -*-
"""方式を替える前の付け替え（`Project.retarget_note_targets`）が、編集を作った解析と照合してから書き換える回帰
（合成の音だけ。素材も RMVPE の重みも要らない）。

- 編集を作った方式と解析の方式がずれた曲で、補正を作った方式へ戻す手当て（`analyze_take(estimator=…)`）が、
  ノート対象の編集を別の区間へ書き換えない（作った解析の記録 = ノートの区切りの指紋と照合する）
- 方式の記録の無い古いアーカイブから戻した編集は、作った解析が分からないので書き換えず、unverified として返す
- 方式の記録のあるアーカイブから戻した編集は、保存した解析がその方式なら付け替える
- `connection`（ノートの組を ID で引く）も、方式を替えた後に同じ境目の組に当たる
"""
import copy
from dataclasses import replace

import pytest

from test_ara_tools import _ok
from test_f0_switch_and_curve import (_daw_with_edit, _note_target_edits, _notes, _song, _spans,  # noqa: F401
                                      eng)


def _shift_gliss_ids(monkeypatch):
    """gliss の解析では頭に短い区間が 1 つ増え、ノートの番号が 1 つずつずれる（方式でノートの番号が変わる曲の真似）。"""
    from vocal_engine.project import store
    real = store.segment_notes

    def shifted(f0r, source="take", **kw):
        ns = real(f0r, source=source, **kw)
        if source != "take" or f0r.estimator != "gliss":
            return ns
        head = replace(ns[0], id="n000", end_sec=ns[0].start_sec + 0.01)
        return [head] + [replace(x, id="n%03d" % (i + 2)) for i, x in enumerate(ns)]

    monkeypatch.setattr(store, "segment_notes", shifted)


def test_returning_to_the_edits_estimator_keeps_note_edits_on_their_notes(eng, tmp_path, monkeypatch):
    """praat で作ったノート対象の編集を据え置いて、解析だけ gliss にした曲（アーカイブを戻した直後・準備の再解析）で、
    補正を作った方式へ戻す手当て（analyze_take(estimator="praat")）をしても、編集は元のノートの区間に当たる。"""
    m, a, md, R = eng
    _shift_gliss_ids(monkeypatch)
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))                             # praat
    _note_target_edits(m)
    p = m._state["project"]
    spans = _spans(p)
    p.analyze(force=True, estimator="gliss", latest=False)            # 編集は据え置きで、解析だけ gliss
    p.estimator_pref = "gliss"
    p.save()
    r = _ok(m.analyze_take(estimator="praat", background=False))
    q = m._state["project"]
    assert q.analysis["take"]["estimator"] == "praat"
    assert _spans(q) == spans                                         # 区間は praat のノートのまま
    assert r["retargeted"] == 0 and r["retarget"]["skipped"]           # 作った解析と違うので書き換えない
    assert sorted(r["retarget"]["unverified"]) == sorted(e.id for e in q.edits if e.target.type == "note")
    assert r["missing_note_targets"]["unverified"] == len(r["retarget"]["unverified"])
    assert [e.target.type for e in q.edits].count("note") == 2         # 番号のまま（praat の解析で元のノート）


def test_new_note_edits_record_the_analysis_they_were_made_on(eng, tmp_path):
    m, a, md, R = eng
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    n = _notes(m)
    _ok(m.shift_pitch(100, note_id=n[1]))
    _ok(m.shift_pitch(-50, start_sec=0.5, end_sec=0.8))               # 範囲の編集には記録を付けない
    p = m._state["project"]
    first, second = p.changesets[-2:]
    basis = [op for op in first.ops if op.get("op") == "basis"]
    assert len(basis) == 1 and basis[0]["estimator"] == "praat" and basis[0]["notes"]
    assert not [op for op in second.ops if op.get("op") == "basis"]
    assert p.edits[0].target.type == "note"                           # 記録は編集の当て方を変えない


def _legacy_archive(m, a, estimator):
    """前の版のアーカイブの真似: changeset に作った解析の記録が無く、方式の記録は estimator（None なら無い）。"""
    arc = copy.deepcopy(_ok(a.ara_archive(["mod-1"]))["archives"]["mod-1"]["archive"])
    for cs in arc["changesets"]:
        cs["ops"] = [op for op in cs["ops"] if op.get("op") != "basis"]
    arc.pop("f0_estimator_version", None)
    if estimator is None:
        arc.pop("f0_estimator", None)
    else:
        arc["f0_estimator"] = estimator
    return arc


def test_old_archive_without_an_estimator_is_not_retargeted(eng, tmp_path, monkeypatch):
    """方式の記録の無いアーカイブの編集は、どの解析で作ったか分からない: 方式を替えても書き換えず、unverified に返す。"""
    m, a, md, R = eng
    _daw_with_edit(m, a, tmp_path)
    arc = _legacy_archive(m, a, None)
    r0 = _ok(a.ara_restore("mod-1", arc))
    p = m._state["project"]
    assert [op for op in p.changesets[0].ops if op.get("op") == "basis"] == [{"op": "basis", "unknown": True}]
    assert r0["state"] == _ok(a.ara_revs())["states"]["mod-1"]       # 記録を足したのは戻した状態のうち
    _ok(m.analyze_take(background=False))
    r = _ok(m.analyze_take(estimator="gliss", background=False))
    q = m._state["project"]
    assert r["retargeted"] == 0 and r["retarget"]["unverified"] == [q.edits[0].id]
    assert "分からない" in r["retarget"]["skipped"]
    assert q.edits[0].target.type == "note"


def test_old_archive_with_its_estimator_is_retargeted(eng, tmp_path):
    """方式の記録のあるアーカイブ（その方式で鳴らしていた）は、保存した解析がその方式なら付け替える。"""
    m, a, md, R = eng
    _daw_with_edit(m, a, tmp_path)
    arc = _legacy_archive(m, a, "praat")
    _ok(a.ara_restore("mod-1", arc))
    p = m._state["project"]
    assert [op for op in p.changesets[0].ops if op.get("op") == "basis"] == [
        {"op": "basis", "estimator": "praat", "version": None}]
    _ok(m.analyze_take(background=False))
    spans = _spans(m._state["project"])
    r = _ok(m.analyze_take(estimator="gliss", background=False))
    q = m._state["project"]
    assert r["retargeted"] == 1 and not r["retarget"]["unverified"]
    assert q.edits[0].target.type == "range" and _spans(q) == spans


def _pairs(p):
    from vocal_engine.project import timing as TM
    by_id = {n.id: n for n in p.take_notes}
    return {(round(by_id[a.id].end_sec, 2), round(by_id[b.id].start_sec, 2)): c for a, b, c, _d in TM.connections(p)}


def test_connection_follows_its_boundary_when_the_estimator_changes(eng, tmp_path, monkeypatch):
    """接続・切り離し（params のノートの組）も、方式を替えた後に同じ境目の組に当たる（番号がずれても別の組に当たらない）。"""
    m, a, md, R = eng
    _shift_gliss_ids(monkeypatch)
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    n = _notes(m)
    p = m._state["project"]
    na, nb = p.note(n[1]), p.note(n[2])
    _ok(m.set_connection(n[1], n[2], False))                          # つながった 2 音目と 3 音目を切り離す
    cut = sorted(k for k, c in _pairs(p).items() if not c)          # フレーズの間（既定で切れている）と、切り離した所
    assert any(k[0] == pytest.approx(na.end_sec, abs=0.01) for k in cut)
    r = _ok(m.analyze_take(estimator="gliss", background=False))
    q = m._state["project"]
    assert r["retarget"]["pairs"] == 1
    (e,) = [e for e in q.edits if e.kind == "connection"]
    assert e.params["by_time"] is True and e.params["pair"] == "note"
    cut2 = sorted(k for k, c in _pairs(q).items() if not c)
    assert len(cut2) == len(cut)                                      # 切れている境目が同じ（ほかの組は切らない）
    for (a0, b0), (a1, b1) in zip(cut, cut2):
        assert a1 == pytest.approx(a0, abs=0.06) and b1 == pytest.approx(b0, abs=0.06)
    # 同じ番号の組（gliss では別の境目）には当てない
    from vocal_engine.project import timing as TM
    assert (n[1], n[2]) not in TM.connection_overrides(q)
    assert nb.start_sec == pytest.approx(na.end_sec, abs=0.01)
