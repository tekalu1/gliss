# -*- coding: utf-8 -*-
"""すべての操作を元に戻す（issue #16。1 曲で 1 本の履歴）と、なだらかさ・結合の残課題（issue #6）。

- 履歴はトラックをまたいで 1 本。別のトラックの操作を戻すと、そのトラックを編集対象にしてから戻す
- トラックの追加・外す・位置・名前・種類・ガイドの指定も戻せる。ミュート／ソロは履歴に入らない
- 歌詞も戻せる（前の歌詞の音素はキャッシュから戻す）
- 複数ノートのピッチのドラッグ（group）は 1 回で戻る。ポップアップの当て直しは 1 回ぶん
- 新しい操作でやり直しの列を捨てる。前の版のプロジェクト（履歴なし）も戻せる
- なだらかさは段差の時刻に掛かる: 結合しても線が変わらない・隙間のある接続で続いている曲線には掛からない
- 結合で境目の無音の挿入を外す（前後はずらさない）・edited・EPS・キャッシュの上限
"""
import json
import os
import shutil

import numpy as np
import pytest

from conftest import CLIP_E, GUIDE, TAKE, needs_clips, needs_model, stem

pytestmark = [needs_clips, needs_model]


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


def _open(m, d):
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    _ok(m.analyze_take())
    return [t["id"] for t in r["session"]["tracks"]]


def _notes(m):
    return [n["id"] for n in _ok(m.list_notes())["notes"]]


def _hist(d):
    with open(os.path.join(d, "session.json"), encoding="utf-8") as f:
        return json.load(f).get("history")


# ================================================================ 1 本の履歴
def test_one_history_across_tracks_switches_track(tmp_path, mcp):
    m, mt = mcp
    d = str(tmp_path / "h1")
    t1, t2 = _open(m, d)
    n1 = _notes(m)[2]
    r = _ok(m.shift_pitch(cents=50, note_id=n1, author="human"))
    assert r["history"]["undo"]["label"] == "ピッチ" and r["history"]["undo"]["track"] == t1
    _ok(mt.select_track(t2))
    _ok(m.analyze_take())
    n2 = _notes(m)[3]
    _ok(m.move_note(ms=-10, note_id=n2, author="human"))
    h = _ok(mt.list_tracks())["history"]
    assert h["undo"]["label"] == "ノートの移動" and h["undo"]["track"] == t2
    # 同じトラックの操作: 切り替えない
    u = _ok(m.undo())
    assert u["undone"]["label"] == "ノートの移動" and u["switched_to"] is None
    assert len(m._state["project"].edits) == 0
    # 別のトラックの操作: そのトラックに切り替えてから戻す
    u = _ok(m.undo())
    assert u["undone"]["track"] == t1 and u["switched_to"] == t1
    assert mt.current_track_id() == t1 and len(m._state["project"].edits) == 0
    assert u["history"]["can_undo"] is False and u["history"]["redo"]["label"] == "ピッチ"
    assert m.undo()["ok"] is False                           # もう無い
    # やり直し: 同じ順に戻る（t2 の操作ではまた切り替わる）
    r1 = _ok(m.redo())
    assert r1["redone"]["label"] == "ピッチ" and r1["switched_to"] is None
    assert len(m._state["project"].edits) == 1
    r2 = _ok(m.redo())
    assert r2["switched_to"] == t2 and len(m._state["project"].edits) >= 1
    assert r2["history"]["can_redo"] is False
    # 履歴は session.json にある（別のプロセス = Claude Code も同じ履歴を読む）
    hs = _hist(d)
    assert [e["label"] for e in hs] == ["ピッチ", "ノートの移動"]
    assert [e["track"] for e in hs] == [t1, t2]


def test_track_operations_and_mute_solo_are_undoable(tmp_path, mcp):
    m, mt = mcp
    d = str(tmp_path / "h2")
    t1, t2 = _open(m, d)
    _ok(mt.set_track(t2, offset_sec=0.3, author="human"))
    _ok(mt.set_track(t2, mute=True, solo=True))
    _ok(mt.set_track(t2, name="guide vox", author="human"))
    r = _ok(mt.add_track(CLIP_E, kind="inst", author="human"))
    t3 = r["track"]
    _ok(mt.set_guide_track(None, author="human"))
    labels = [e["label"] for e in _hist(d)]
    assert labels == ["トラックの位置", "トラックのミュート・トラックのソロ",
                      "トラックの名前", "トラックの追加", "ガイドの指定"]
    # ガイドの指定を戻す
    u = _ok(m.undo())
    assert u["undone"]["label"] == "ガイドの指定" and u["switched_to"] == t2   # 操作したトラックへ
    assert _ok(mt.list_tracks())["guide"] == t2
    # トラックの追加を戻す（伴奏なので編集対象は変えない）
    u = _ok(m.undo())
    assert u["undone"]["label"] == "トラックの追加" and u["switched_to"] is None
    assert t3 not in [t["id"] for t in _ok(mt.list_tracks())["tracks"]]
    # 名前を戻す（編集対象はもう t2）。ミュート／ソロは今のまま
    _ok(mt.select_track(t1))
    u = _ok(m.undo())
    tr = {t["id"]: t for t in _ok(mt.list_tracks())["tracks"]}
    assert u["switched_to"] == t2 and tr[t2]["name"] == stem(GUIDE)
    assert tr[t2]["mute"] is True and tr[t2]["solo"] is True
    # ミュート／ソロと位置を順に戻す
    _ok(m.undo())
    tr = {t["id"]: t for t in _ok(mt.list_tracks())["tracks"]}
    assert tr[t2]["mute"] is False and tr[t2]["solo"] is False
    _ok(m.undo())
    tr = {t["id"]: t for t in _ok(mt.list_tracks())["tracks"]}
    assert tr[t2]["offset_sec"] == 0.0 and tr[t2]["mute"] is False
    # やり直すと足したトラックが同じ id・同じ位置で戻る
    for _ in range(5):
        _ok(m.redo())
    tr = [t["id"] for t in _ok(mt.list_tracks())["tracks"]]
    assert tr == [t1, t2, t3]
    # トラックを外す → 戻す（同じ id・同じ並び・編集もそのまま）
    _ok(mt.select_track(t1))
    _ok(m.analyze_take())
    _ok(m.shift_pitch(cents=20, note_id=_notes(m)[1], author="human"))
    _ok(mt.select_track(t2))
    r = _ok(mt.remove_track(t1, author="human"))
    assert t1 not in [t["id"] for t in r["session"]["tracks"]]
    u = _ok(m.undo())
    assert u["undone"]["label"] == "トラックを外す" and u["switched_to"] == t1
    assert [t["id"] for t in _ok(mt.list_tracks())["tracks"]] == [t1, t2, t3]
    assert len(m._state["project"].edits) == 1


def test_track_order_is_undoable(tmp_path, mcp):
    """トラックの並び順（issue #38）: set_track(index) で動かす・取り消しの履歴に入る・端に丸める・同じ位置は入れない。"""
    m, mt = mcp
    d = str(tmp_path / "order")
    t1, t2 = _open(m, d)
    t3 = _ok(mt.add_track(CLIP_E, kind="inst", author="human"))["track"]
    ids = lambda: [t["id"] for t in _ok(mt.list_tracks())["tracks"]]   # noqa: E731
    assert ids() == [t1, t2, t3]
    r = _ok(mt.set_track(t3, index=0, author="human"))
    assert [t["id"] for t in r["session"]["tracks"]] == [t3, t1, t2]
    assert r["reopened"] is False                            # 並びは音・ガイドとの対応を変えない
    assert _hist(d)[-1]["label"] == "トラックの順番"
    n = len(_hist(d))
    _ok(mt.set_track(t3, index=0, author="human"))           # 変わらない: 履歴に入れない
    assert len(_hist(d)) == n
    _ok(mt.set_track(t1, index=99, author="human"))          # 範囲の外は端に
    assert ids() == [t3, t2, t1]
    with open(os.path.join(d, "session.json"), encoding="utf-8") as f:
        assert [t["id"] for t in json.load(f)["tracks"]] == [t3, t2, t1]
    u = _ok(m.undo())
    assert u["undone"]["label"] == "トラックの順番" and ids() == [t3, t1, t2]
    _ok(m.undo())
    assert ids() == [t1, t2, t3]
    assert mt.current_track_id() == t1                       # 伴奏の並びを戻しても編集対象は変わらない
    _ok(m.redo())
    assert ids() == [t3, t1, t2]
    # 名前と一緒に変えたら 1 回の取り消し
    _ok(mt.set_track(t2, index=0, name="guide vox", author="human"))
    assert _hist(d)[-1]["label"] == "トラックの名前・トラックの順番"
    _ok(m.undo())
    tr = _ok(mt.list_tracks())["tracks"]
    assert [t["id"] for t in tr] == [t3, t1, t2] and tr[2]["name"] == stem(GUIDE)


def test_group_popup_and_redo_tail(tmp_path, mcp):
    m, mt = mcp
    d = str(tmp_path / "h3")
    _open(m, d)
    ns = _notes(m)
    # 複数ノートのピッチのドラッグ = 1 回
    _ok(m.shift_pitch(cents=30, note_id=ns[1], group="g1", author="human"))
    _ok(m.shift_pitch(cents=30, note_id=ns[2], group="g1", author="human"))
    assert len(_hist(d)) == 1 and len(_hist(d)[0]["changesets"]) == 2
    # ポップアップの当て直し = 1 回ぶん（前の値の項目は入れ替わる）
    r1 = _ok(m.set_transition(value=0.2, note_ids=[ns[1]], author="human"))
    _ok(m.set_transition(value=0.8, note_ids=[ns[1]], replaces=r1["changeset"], author="human"))
    assert [e["label"] for e in _hist(d)] == ["ピッチ", "なだらかさ"]
    _ok(m.undo())
    u = _ok(m.undo())
    assert u["undone"]["label"] == "ピッチ" and len(m._state["project"].edits) == 0
    # 新しい操作を入れるとやり直しの列は捨てる
    _ok(m.shift_pitch(cents=-10, note_id=ns[3], author="human"))
    assert m.redo()["ok"] is False
    assert [e["label"] for e in _hist(d)] == ["ピッチ"]
    p = m._state["project"]
    assert all(c.discarded for c in p.changesets if c.undone)


def test_lyrics_are_undoable(tmp_path, mcp):
    m, mt = mcp
    d = str(tmp_path / "h4")
    _open(m, d)
    p = m._state["project"]
    inferred = p.lyrics_entries("take")
    _ok(m.set_lyrics(entries=[{"start_sec": 0.5, "end_sec": 1.5, "text": "あいうえ"}],
                     reanalyze=False, author="human"))
    _ok(m.set_lyrics(entries=[{"start_sec": 0.5, "end_sec": 1.5, "text": "あいうえお"}],
                     reanalyze=False, author="human"))
    assert [e["label"] for e in _hist(d)] == ["歌詞", "歌詞"]
    u = _ok(m.undo())
    assert u["undone"]["label"] == "歌詞" and p.lyrics_entries("take")[0]["text"] == "あいうえ"
    _ok(m.undo())
    assert p.lyrics_entries("take") == inferred
    _ok(m.redo())
    assert p.lyrics_entries("take")[0]["text"] == "あいうえ"
    # project.json にも残る（開き直しても同じ）
    from vocal_engine.project import Project
    assert Project(p.dir).load().lyrics_entries("take")[0]["text"] == "あいうえ"
    # 同じ歌詞なら履歴に入れない
    n = len(_hist(d))
    _ok(m.set_lyrics(entries=[{"start_sec": 0.5, "end_sec": 1.5, "text": "あいうえ"}],
                     reanalyze=False))
    assert len(_hist(d)) == n


def test_old_project_without_history_can_be_undone(tmp_path, mcp):
    """前の版（session.json も履歴も無い）で入れた編集も、開いてから Ctrl+Z で戻せる。"""
    from vocal_engine.project import Project
    from vocal_engine.project.model import Target
    m, mt = mcp
    d = str(tmp_path / "old")
    p = Project.open(TAKE, GUIDE, project_dir=d)
    p.analyze()
    ns = [n.id for n in p.take_notes if n.kind == "note"]
    p.apply_edits([{"kind": "pitch_shift", "target": Target.note(ns[1]), "params": {"cents": 30.0}}],
                  label="note %s を +30 セント" % ns[1])
    p.apply_edits([{"kind": "pitch_shift", "target": Target.note(ns[2]), "params": {"cents": 40.0}}],
                  label="note %s を +40 セント" % ns[2])
    _ok(m.open_project(TAKE, project_dir=d))
    h = _ok(mt.list_tracks())["history"]
    assert h["can_undo"] and h["undo"]["label"] == "ピッチ" and h["size"] == 2
    _ok(m.undo())
    assert len(m._state["project"].edits) == 1
    # 履歴を知らない経路（プロジェクトを直接）で入った編集も、次の Ctrl+Z で戻る
    q = m._state["project"]
    q.apply_edits([{"kind": "pitch_shift", "target": Target.note(ns[3]), "params": {"cents": 5.0}}])
    u = _ok(m.undo())
    assert u["undone"]["label"] == "ピッチ"
    assert [e.params["cents"] for e in q.edits] == [30.0]


def test_explicit_changeset_undo_without_session(tmp_path):
    """セッションの無いプロジェクト（DAW 連携のサンプル列など）は今までどおりプロジェクトの undo。"""
    from vocal_engine import mcp_server as m
    from vocal_engine.project import Project
    p = Project.open(TAKE, GUIDE, project_dir=str(tmp_path / "ns"))
    p.analyze()
    m._state.update(project=p, session=None, track=None)
    try:
        nid = [n.id for n in p.take_notes if n.kind == "note"][1]
        r = _ok(m.shift_pitch(cents=10, note_id=nid))
        assert r["history"] is None
        u = _ok(m.undo())
        assert u["undone"]["id"] == r["changeset"]
    finally:
        m._state.update(project=None, session=None, track=None)


# ================================================================ なだらかさ（#6）
@pytest.fixture
def plain(tmp_path):
    from vocal_engine.project import Project
    p = Project.open(TAKE, GUIDE, project_dir=str(tmp_path / "plain"))
    p.analyze()
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)


def _plain_mcp(p):
    from vocal_engine import mcp_server as m
    m._state.update(project=p, session=None, track=None)
    m._invalidate_renderer()
    return m


def _offsets(p):
    from vocal_engine.project.pitch import pitch_model
    from vocal_engine.view.export_data import cents_offset
    base, segs, lay, trs = pitch_model(p)
    t = p.take_f0.times
    return cents_offset(base, t), cents_offset(segs, t), trs


def _voiced(p):
    f0r = p.take_f0
    return np.asarray(f0r.voiced, dtype=bool) & (np.asarray(f0r.f0) > 0)


def test_merge_keeps_the_smoothing(plain):
    """結合しても線は変わらない（なだらかさは段差の時刻に掛かる。以前は最大 50 セント変わった）。"""
    from vocal_engine.project.model import Target
    m = _plain_mcp(plain)
    plain.apply_edits([{"kind": "pitch_shift", "target": Target.note("n005"),
                        "params": {"cents": 100.0}}])
    m.set_transition(value=0.8, note_a="n004", note_b="n005", author="human")
    _, off0, trs0 = _offsets(plain)
    tr0 = next(t for t in trs0 if (t.a, t.b) == ("n004", "n005"))
    assert tr0.active and tr0.set_by_user
    assert _ok(m.merge_notes("n004", "n005", author="human"))
    _, off1, trs1 = _offsets(plain)
    v = _voiced(plain)
    assert np.max(np.abs(off1[v] - off0[v])) < 1e-6
    st = [t for t in trs1 if t.kind == "step" and t.a == "n004"]
    assert len(st) == 1 and abs(st[0].ta - tr0.ta) < 1e-9 and st[0].set_by_user
    assert abs(st[0].value - 0.8) < 1e-9
    # 分割し直しても同じ（境目に戻る）
    assert _ok(m.split_note(sec=tr0.ta, author="human"))
    _, off2, _ = _offsets(plain)
    assert np.max(np.abs(off2[v] - off0[v])) < 1e-6
    # ノートの中の段差も値を変えられる（選んだノート）
    _ok(m.merge_notes("n004", [n.id for n in plain.take_notes if n.id.startswith("n004@")][0],
                      author="human"))
    r = _ok(m.set_transition(value=0.0, note_ids=["n004"], author="human"))
    assert r["pairs"] >= 1
    base, off3, _ = _offsets(plain)
    assert np.max(np.abs(off3[v] - base[v])) < 1e-6          # 0 = 段差


def test_continuous_curve_over_a_gap_is_not_smoothed(tmp_path):
    """隙間のある接続で 1 本の曲線が両側を覆っても、続いている曲線はつなぎの対象にしない。"""
    from conftest import CLIP_A
    from vocal_engine.project import Project
    from vocal_engine.project import timing as TM
    from vocal_engine.project.model import Target
    plain = Project.open(CLIP_A, None, project_dir=str(tmp_path / "a"))
    plain.analyze()
    rows = [(a, b) for a, b, c, _ in TM.connections(plain) if c and b.start_sec - a.end_sec > 0.03]
    assert rows, "隙間のある接続が無い"
    a, b = rows[0]
    L = b.end_sec - a.start_sec
    plain.apply_edits([{"kind": "pitch_curve", "target": Target.range(a.start_sec, b.end_sec),
                        "params": {"points": [[0.0, 0.0], [0.5 * L, 200.0], [L, 0.0]]}}])
    base, off, trs = _offsets(plain)
    tr = next(t for t in trs if (t.a, t.b) == (a.id, b.id))
    assert abs(tr.delta) < 1e-6 and not tr.active
    v = _voiced(plain)
    assert np.max(np.abs(off[v] - base[v])) < 1e-6


def test_small_differences_are_not_steps(plain):
    from vocal_engine.project.model import Target
    from vocal_engine.project.pitch import EPS_CENTS
    assert EPS_CENTS == pytest.approx(0.1)
    plain.apply_edits([{"kind": "pitch_shift", "target": Target.note("n005"),
                        "params": {"cents": 0.05}}])
    base, off, trs = _offsets(plain)
    assert not any(t.active for t in trs)


def test_merge_keeps_the_silence_at_the_boundary(plain):
    """分割 → 切り離して左を縮める → 結合しても手動の無音と両側の位置を保つ。"""
    from vocal_engine.project import timing as TM
    m = _plain_mcp(plain)
    n = plain.note("n006")
    t = round(0.5 * (n.start_sec + n.end_sec), 3)
    rid = _ok(m.split_note(sec=t, author="human"))["right"]
    _ok(m.set_connection("n006", rid, False, author="human"))
    _ok(m.stretch(ratio=0.8, note_id="n006", author="human"))
    sil = [e for e in plain.edits if e.kind == "silence" and abs(e.target.start_sec - t) < 0.01]
    assert sil
    tm0 = TM.current_map(plain)
    a0, b0 = tm0.at(n.start_sec, "right"), tm0.at(n.end_sec, "left")
    r = _ok(m.merge_notes("n006", rid, author="human"))
    assert r["removed_silence"] is False
    assert [e for e in plain.edits if e.kind == "silence" and abs(e.target.start_sec - t) < 0.01]
    tm1 = TM.current_map(plain)
    assert abs(tm1.at(n.start_sec, "right") - a0) < 1e-6
    assert abs(tm1.at(n.end_sec, "left") - b0) < 1e-6
    assert abs(tm1.at(t, "left") - tm0.at(t, "left")) < 1e-9
    assert abs(tm1.at(t, "right") - tm0.at(t, "right")) < 1e-9
    # 後ろのノートは 1 サンプルも動かない
    nxt = plain.note("n007")
    assert abs(tm1.at(nxt.start_sec) - tm0.at(nxt.start_sec)) < 1e-9


def test_edited_only_where_values_changed(plain):
    from vocal_engine.project.model import Target
    from vocal_engine.view.export_data import export_view_data
    plain.apply_edits([{"kind": "pitch_shift", "target": Target.note("n005"),
                        "params": {"cents": 0.05}}])
    plain.apply_edits([{"kind": "pitch_shift", "target": Target.note("n010"),
                        "params": {"cents": 80.0}}])
    r = export_view_data(plain)
    with open(r["path"], encoding="utf-8") as f:
        d = json.load(f)
    by = {n["id"]: n for n in d["notes"]}
    assert by["n010"]["edited"] is True
    assert by["n005"]["edited"] is False                      # 聞き分けられない量
    assert by["n002"]["edited"] is False                      # 離れたノート


def test_auto_width_cache_is_bounded(plain, monkeypatch):
    from vocal_engine.project import pitch as PI
    monkeypatch.setattr(PI, "AUTO_CACHE_MAX", 3)
    PI.transitions(plain)
    assert len(plain._auto_width_cache[1]) <= 3
    PI.forget_cache(plain)
    assert plain._auto_width_cache is None


def test_view_data_has_draws_for_the_pencil_preview(plain):
    """鉛筆の描き直しのプレビュー用: 鉛筆を当てる前の曲線と線の範囲（覆われる線を外して描くため）。"""
    from vocal_engine.view.export_data import export_view_data
    m = _plain_mcp(plain)
    n = plain.note("n010")
    a, b = n.start_sec + 0.02, n.end_sec - 0.02
    _ok(m.set_pitch_curve(points=[[a, 64.0], [b, 64.5]], mode="draw", author="human"))
    with open(export_view_data(plain)["path"], encoding="utf-8") as f:
        d = json.load(f)
    assert len(d["draws"]) == 1 and d["draws"][0]["lo"] < d["draws"][0]["t0"]
    nd = d["f0"]["take_nodraw_midi"]
    assert len(nd) == len(d["f0"]["take_midi"])


def test_entries_undone_elsewhere_are_skipped(tmp_path, mcp):
    """別のプロセスが project.json で取り消した操作は、Ctrl+Z 1 回を空振りさせずに飛ばす。"""
    from vocal_engine.project import Project
    m, mt = mcp
    d = str(tmp_path / "h5")
    _open(m, d)
    ns = _notes(m)
    _ok(m.shift_pitch(cents=30, note_id=ns[1], author="human"))
    r2 = _ok(m.shift_pitch(cents=40, note_id=ns[2], author="ai"))
    other = Project(d).load()                  # 別のプロセス（前の版のエンジンなど）
    other.undo(r2["changeset"])
    u = _ok(m.undo())
    assert u["undone"]["author"] == "human"    # 外で戻された ai の操作は飛ばした
    assert len(m._state["project"].edits) == 0
    assert [e["undone"] for e in _hist(d)] == [True, True]
