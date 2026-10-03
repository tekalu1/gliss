# -*- coding: utf-8 -*-
"""DAW（VST3 + ARA 2 のプラグイン）のツール（`vocal_engine/mcp_ara.py`。engine/docs/MCP.md §3-4）。

音は合成（倍音のある正弦波にビブラートを掛けた歌声もどき）。ピッチ検出は重みの要らない Praat。
プラグイン（C++）のキャッシュの当て方（reset → restore → windows）をこのファイルの `Cache` で真似て、
`render_region` の全体（＝書き出し）と比べる。

- 同じソースの 2 つの修飾が別の編集を持つ
- `ara_render_dirty` を編集ごとに当てた結果が `render_region` の全体と一致する（取り消し・やり直しも）
- `since` 違い・エンジンの作り直しで `reset`。`max_sec` で分けても同じ結果（途中で編集が入っても）
- `ara_archive` → 別の作業場所で `ara_restore` → 同じ PCM。中身の違う素材は `mismatch`
- `ara_sync` で位置を変えると `reopened`・履歴に入らない。`ara_*` の後の `undo` が DAW の操作を戻さない
- `ara_archive` / `ara_revs` がジョブ中（エンジンのロックを握られている間）も待たない
- ソースの書き直し（同じ音・違う音）、複製（clone_of）、外して足し直す
- stdio 越しの 1 本
"""
import json
import os
import threading
import time

import numpy as np
import pytest
import soundfile as sf

from conftest import VENV_PY

SR = 44100
# (頭の秒, 長さ, MIDI)。かたまり（窓）が分かれるように 0.6 秒あける
NOTES = [(0.5, 0.55, 60), (1.65, 0.55, 64), (2.8, 0.55, 67), (3.95, 0.55, 65), (5.1, 0.55, 62)]
DUR = 6.2


def _voice(stereo=False, transpose=0.0, seed=0, gain=0.25):
    n = int(SR * DUR)
    y = np.zeros(n)
    for st, d, midi in NOTES:
        a, b = int(st * SR), int((st + d) * SR)
        t = np.arange(b - a) / SR
        f0 = 440.0 * 2 ** ((midi + transpose - 69) / 12)
        vib = 30 * np.sin(2 * np.pi * 5.5 * t) * np.clip((t - 0.12) / 0.1, 0, 1)
        ph = 2 * np.pi * np.cumsum(f0 * 2 ** (vib / 1200)) / SR
        s = sum((0.6 ** k) * np.sin((k + 1) * ph) for k in range(8))
        env = np.minimum(1, np.minimum(t / 0.03, (d - t) / 0.04))
        y[a:b] += gain * s * env
    y += np.random.default_rng(seed).normal(scale=1e-4, size=n)
    if stereo:
        return np.stack([y, 0.8 * np.roll(y, 3)], axis=1)
    return y


def _wav(path, x, subtype="FLOAT"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sf.write(path, np.asarray(x, dtype="float32"), SR, subtype=subtype)
    return str(path)


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _prep_off():
    """裏の準備が止まっているか（既定。GLISS_TEST_PREP=1 なら裏で解析が先に済むことがある）。"""
    return os.environ.get("VOCAL_ENGINE_PREP") == "0"


class Cache:
    """プラグインのキャッシュの真似（design §4-2）: 原音の上に restore → windows を当てる。"""

    def __init__(self, path):
        self.orig = sf.read(path, dtype="float32", always_2d=True)[0]
        self.buf = self.orig.copy()
        self.rev = None
        self.calls = []

    def apply(self, r):
        if r["reset"]:
            self.buf = self.orig.copy()
        for s0, k in r["restore"]:
            self.buf[s0:s0 + k] = self.orig[s0:s0 + k]
        if r["windows"]:
            raw = np.fromfile(r["path"], dtype="<f4")
            ch = r["channels"]
            assert raw.size == sum(w["frames"] for w in r["windows"]) * ch
            for w in r["windows"]:
                a = w["byte_offset"] // 4
                self.buf[w["start_frame"]:w["start_frame"] + w["frames"]] = \
                    raw[a:a + w["frames"] * ch].reshape(-1, ch)
        self.rev = r["rev"]
        self.calls.append(r)

    def sync(self, a, ara_id, **kw):
        for _ in range(200):
            r = _ok(a.ara_render_dirty(ara_id, since=self.rev, **kw))
            self.apply(r)
            if not r["more"]:
                return r
        raise AssertionError("more が終わらない")


@pytest.fixture
def ara(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m     # 先に読む（mcp_ara などは mcp_server の末尾から読まれる）
    from vocal_engine import mcp_ara as a
    from vocal_engine import mcp_document as md
    from vocal_engine.analysis import f0 as F
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))   # 人の作業場所に作らない
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    F.set_preferred_estimator("praat")           # 重みが無くても解析できる
    yield m, a
    F.set_preferred_estimator(None)
    md._clear()
    a._reset_render()


def _open(a, key="doc-1"):
    return _ok(a.ara_open(key, name="曲"))


def _add(a, ara_id, path, **kw):
    kw.setdefault("source_id", "src-" + os.path.basename(path))
    kw.setdefault("name", ara_id)
    return _ok(a.ara_set_modification(ara_id, path, **kw))


def _select_and_analyze(m, tid):
    from vocal_engine import mcp_tracks as mt
    if mt.current_track_id() != tid:
        _ok(mt.select_track(tid))
    _ok(m.analyze_take(background=False))
    return [n["id"] for n in _ok(m.list_notes(kind="note"))["notes"]]


def _ref(a, ara_id):
    """修飾のトラックの編集を当てた音の全体（`render_region`。チャンネルはソースのまま）を float32 で。"""
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_tracks as mt
    from vocal_engine.render.region import RegionRenderer, render_region
    s = m._state["session"]
    t = s.find_ara(ara_id)
    p, _ = mt._track_project(s, t)
    rr = RegionRenderer.for_project(p, channels="all")
    y, info = render_region(p, renderer=rr)
    return y.astype("float32")


def _same(cache, a, ara_id):
    ref = _ref(a, ara_id)
    assert cache.buf.shape == ref.shape
    d = np.abs(cache.buf.astype("float64") - ref.astype("float64")).max()
    assert np.array_equal(cache.buf, ref), "最大の差 %g" % d


# ================================================================ 開く・修飾
def test_open_creates_an_ara_document(ara, tmp_path):
    m, a = ara
    from vocal_engine import mcp_document as md
    r = _open(a)
    assert r["created"] and r["document"]["kind"] == "ara" and r["document"]["dirty"] is False
    assert os.path.normcase(r["dir"]) == os.path.normcase(str(tmp_path / "work" / "ara" / "doc-1"))
    s = m._state["session"]
    assert s.ara and s.tracks == []
    assert _ok(a.ara_open("doc-1"))["opened"] is False           # 同じ鍵: 何もしない
    with pytest.raises(Exception):
        from vocal_engine.project.document import ara_work_dir
        ara_work_dir("../x")
    assert a.ara_open("../evil")["ok"] is False
    # 保存は DAW がする（.gliss にしない）
    assert md.save_project(str(tmp_path / "x.gliss"))["ok"] is False
    assert not os.path.exists(tmp_path / "x.gliss")
    # 開いていない間は ara_* はエラー
    md._clear()
    assert a.ara_revs()["ok"] is False and a.ara_archive()["ok"] is False


def test_same_source_two_modifications_have_separate_edits(ara, tmp_path):
    """同じ AudioSource の 2 つの修飾（DAW で「固有にする」など）は、同じ WAV の別のトラック・別の編集。"""
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice(stereo=True))
    _open(a)
    r1 = _add(a, "mod-A", src, offset_sec=10.0, group="Vox")
    r2 = _add(a, "mod-B", src, offset_sec=30.0, group="Vox")
    assert r1["created"] and r2["created"] and r1["selected"] and not r2["selected"]
    t1, t2 = r1["track"]["id"], r2["track"]["id"]
    assert t1 != t2 and r1["track"]["project_dir"] != r2["track"]["project_dir"]
    tracks = {t["id"]: t for t in r2["session"]["tracks"]}
    assert tracks[t1]["ara_id"] == "mod-A" and tracks[t2]["group"] == "Vox"
    assert tracks[t1]["offset_sec"] == 10.0 and tracks[t2]["offset_sec"] == 30.0

    n1 = _select_and_analyze(m, t1)
    _ok(m.shift_pitch(200, note_id=n1[0]))
    n2 = _select_and_analyze(m, t2)
    _ok(m.shift_pitch(-300, note_id=n2[2]))
    _ok(m.shift_pitch(100, note_id=n2[4]))
    arcs = _ok(a.ara_archive())["archives"]
    assert len(arcs["mod-A"]["archive"]["changesets"]) == 1
    assert len(arcs["mod-B"]["archive"]["changesets"]) == 2
    ca, cb = Cache(src), Cache(src)
    ca.sync(a, "mod-A")
    cb.sync(a, "mod-B")
    _same(ca, a, "mod-A")
    _same(cb, a, "mod-B")
    assert not np.array_equal(ca.buf, cb.buf)
    # 窓はそれぞれの編集の場所だけ（mod-A は 1 つ目のノート、mod-B は 3・5 つ目）
    wa = [w["start_frame"] / SR for w in ca.calls[0]["windows"]]
    wb = [w["start_frame"] / SR for w in cb.calls[0]["windows"]]
    assert len(wa) == 1 and wa[0] < 0.6 and len(wb) == 2 and all(x > 2.0 for x in wb)


def test_render_dirty_per_edit_matches_the_full_render(ara, tmp_path):
    """編集（と取り消し・やり直し）のたびに since で差分を当てた結果が、毎回 render_region の全体と一致する。"""
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice(stereo=True))
    _open(a)
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)
    c = Cache(src)
    r = c.sync(a, "mod-A")
    assert r["reset"] and r["windows"] == [] and r["restore"] == [] and r["path"] is None   # 編集なし = 原音
    assert r["sr"] == SR and r["channels"] == 2 and r["source_frames"] == int(SR * DUR)
    assert np.array_equal(c.buf, c.orig)
    _same(c, a, "mod-A")

    n1a, n1b = NOTES[1][0], NOTES[1][0] + NOTES[1][1]
    steps = [
        lambda: m.shift_pitch(150, note_id=notes[0]),
        lambda: m.stretch(1.2, note_id=notes[3]),
        lambda: m.set_pitch_curve([[0.0, 0.0], [0.2, 80.0], [0.45, -60.0]], note_id=notes[2]),
        lambda: m.split_note(round((n1a + n1b) / 2, 3), note_id=notes[1]),
        lambda: m.shift_pitch(-120, note_id=notes[4]),
        lambda: m.undo(),
        lambda: m.undo(),
        lambda: m.redo(),
        lambda: m.shift_pitch(50, note_id=notes[0]),
    ]
    total = int(SR * DUR)
    for i, step in enumerate(steps):
        _ok(step())
        r = c.sync(a, "mod-A")
        assert r["reset"] is False, i
        _same(c, a, "mod-A")
        changed = sum(k for _, k in r["restore"])
        assert changed < total, i                  # 変わった範囲だけ（全体を作り直さない）
    # 編集が変わっていなければ空（版も同じ）
    r = c.sync(a, "mod-A")
    assert r["restore"] == [] and r["windows"] == [] and r["rev"] == c.calls[-2]["rev"]
    # ara_revs の版 = 最後に渡した版
    revs = _ok(a.ara_revs())
    assert revs["revs"]["mod-A"] == c.rev and revs["track_ids"]["mod-A"] == tid


def test_render_dirty_of_a_track_that_is_not_being_edited(ara, tmp_path):
    """編集対象でないトラックも、編集対象を変えずに音を作れる（ディスクのプロジェクトを読む）。"""
    m, a = ara
    from vocal_engine import mcp_tracks as mt
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    src2 = _wav(tmp_path / "src" / "b.wav", _voice(transpose=2, seed=1))
    _open(a)
    t1 = _add(a, "mod-A", src)["track"]["id"]
    t2 = _add(a, "mod-B", src2)["track"]["id"]
    n1 = _select_and_analyze(m, t1)
    _ok(m.shift_pitch(200, note_id=n1[1]))
    _select_and_analyze(m, t2)                   # 編集対象を B に
    c = Cache(src)
    c.sync(a, "mod-A")
    assert mt.current_track_id() == t2
    _same(c, a, "mod-A")
    # プロジェクトがまだ無い修飾（選ばれていない・準備前）は "empty"
    _add(a, "mod-C", src2)
    assert _ok(a.ara_revs())["revs"]["mod-C"] == "empty"
    r = _ok(a.ara_render_dirty("mod-C"))
    assert r["rev"] == "empty" and r["windows"] == [] and r["reset"]


def test_since_mismatch_and_engine_restart_reset(ara, tmp_path):
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    _open(a)
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)
    _ok(m.shift_pitch(100, note_id=notes[1]))
    c = Cache(src)
    r = c.sync(a, "mod-A")
    assert r["reset"] and len(r["windows"]) == 1
    _ok(m.shift_pitch(100, note_id=notes[3]))
    # 手元の版が違う（取りこぼした・別の版）: reset で全部の窓
    r = _ok(a.ara_render_dirty("mod-A", since="nope"))
    assert r["reset"] and len(r["windows"]) == 2
    c2 = Cache(src)
    c2.apply(r)
    _same(c2, a, "mod-A")
    # エンジンを作り直した（このプロセスの状態が消えた）: 手元の版を渡しても reset
    a._reset_render()
    r = _ok(a.ara_render_dirty("mod-A", since=c2.rev))
    assert r["reset"] and len(r["windows"]) == 2
    # backend が違えば reset
    r2 = _ok(a.ara_render_dirty("mod-A", since=r["rev"], backend="psola"))
    assert r2["reset"]
    # 解析が変わった（解析し直した）: reset
    c3 = Cache(src)
    c3.sync(a, "mod-A")
    _ok(m.analyze_take(force=True, background=False))
    r3 = _ok(a.ara_render_dirty("mod-A", since=c3.rev))
    assert r3["reset"]
    c3.apply(r3)
    _same(c3, a, "mod-A")


def test_max_sec_splits_into_several_calls_with_the_same_result(ara, tmp_path):
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice(stereo=True))
    _open(a)
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)
    for k in (0, 2, 4):                          # 隣のノートは窓が重なってまとまるので 1 つおき
        _ok(m.shift_pitch(50 + 30 * k, note_id=notes[k]))
    whole = Cache(src)
    r = whole.sync(a, "mod-A")
    assert not r["more"] and len(r["windows"]) == 3

    a._reset_render()                            # 最初から（reset）を小分けで
    part = Cache(src)
    r = _ok(a.ara_render_dirty("mod-A", max_sec=0.01))
    assert r["more"] and len(r["windows"]) == 1 and "~" in r["rev"]
    part.apply(r)
    # 途中の版は ara_revs の版と違う（プラグインは続けて呼ぶ）
    assert _ok(a.ara_revs())["revs"]["mod-A"] != part.rev
    r = _ok(a.ara_render_dirty("mod-A", since=part.rev, max_sec=0.01))
    assert r["more"] and not r["reset"] and len(r["windows"]) == 1
    part.apply(r)
    # 小分けの途中で編集が入っても、続きの結果は全体と同じ
    _ok(m.shift_pitch(-200, note_id=notes[0]))
    part.sync(a, "mod-A", max_sec=0.01)
    _same(part, a, "mod-A")
    assert sum(1 for x in part.calls if x["more"]) >= 3
    assert all(len(x["windows"]) <= 1 for x in part.calls)
    whole.sync(a, "mod-A")
    assert np.array_equal(part.buf, whole.buf)


# ================================================================ アーカイブ
def test_archive_restores_the_same_sound_in_another_work_place(ara, tmp_path, monkeypatch):
    m, a = ara
    from vocal_engine import mcp_document as md
    src = _wav(tmp_path / "src" / "a.wav", _voice(stereo=True))
    _open(a, "doc-1")
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)
    _ok(m.shift_pitch(150, note_id=notes[0]))
    _ok(m.stretch(1.15, note_id=notes[2]))
    _ok(m.shift_pitch(-80, note_id=notes[4]))
    _ok(m.undo())
    n_edits = len(m._state["project"].edits)      # 伸縮は後ろをずらさない組み直しで複数の編集になる
    n_cs = len(m._state["project"].changesets)
    c1 = Cache(src)
    c1.sync(a, "mod-A")
    ar = _ok(a.ara_archive(["mod-A"]))
    arc = ar["archives"]["mod-A"]["archive"]
    blob = json.dumps(ar)                        # JSON にそのまま書ける（DAW のアーカイブ）
    assert len(blob) < 50_000
    assert arc["guide"] is None and arc["take"]["frames"] == int(SR * DUR)
    for k in ("dir", "analysis", "updated_at"):
        assert k not in arc

    # 別の PC（別の作業場所・プラグインが書き直したソースの WAV）で開き直す
    md._clear()
    a._reset_render()
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work2"))
    src2 = _wav(tmp_path / "src2" / "a.wav", sf.read(src, dtype="float32")[0])
    _open(a, "doc-1")
    t2 = _add(a, "mod-A", src2)["track"]["id"]
    r = _ok(a.ara_restore("mod-A", json.loads(blob)["archives"]["mod-A"]["archive"]))
    assert r["mismatch"] is False and r["edits"] == n_edits and r["changesets"] == n_cs == 3 and r["reopened"]
    # 解析の前（別の PC には解析のキャッシュが無い）: ロックを握って解析を待たず、原音のまま返す
    c2 = Cache(src2)
    r = c2.sync(a, "mod-A")
    if _prep_off():
        assert r["analysis_pending"] is True and r["windows"] == [] and np.array_equal(c2.buf, c2.orig)
        pending_rev = c2.rev
        assert _ok(a.ara_revs())["revs"]["mod-A"] == pending_rev
    _select_and_analyze(m, t2)
    if _prep_off():
        assert _ok(a.ara_revs())["revs"]["mod-A"] != pending_rev     # 解析が済むと版が変わる → 呼び直す
        r = c2.sync(a, "mod-A")
        assert r["reset"] and r["analysis_pending"] is False and r["windows"]
    c2.sync(a, "mod-A")                           # 裏の準備があるときは、ここで解析の後の版を取る
    assert np.array_equal(c1.buf, c2.buf)
    # 戻した編集は Ctrl+Z の列に入らない（DAW の読み込みを Gliss で取り消さない）
    from vocal_engine import mcp_tracks as mt
    assert mt.history_summary()["can_undo"] is False
    # 取り消した changeset も戻っているので、やり直せる
    assert m.redo()["ok"] is False                # 曲の履歴には無い
    assert len(_ok(m.list_changes(include_undone=True))["changesets"]) == 3


def test_restore_with_other_material_is_a_mismatch(ara, tmp_path):
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    other = _wav(tmp_path / "src" / "b.wav", _voice(transpose=1))
    shorter = _wav(tmp_path / "src" / "c.wav", _voice()[: SR * 3])
    _open(a)
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)
    _ok(m.shift_pitch(150, note_id=notes[0]))
    arc = _ok(a.ara_archive())["archives"]["mod-A"]["archive"]
    tb = _add(a, "mod-B", other)["track"]
    _add(a, "mod-C", shorter)
    for aid in ("mod-B", "mod-C"):
        r = _ok(a.ara_restore(aid, arc))
        assert r["mismatch"] is True and r["reason"], aid
    pj = os.path.join(tb["project_dir"], "project.json")    # 触らない（裏の準備が作っていても編集は無い）
    assert not os.path.exists(pj) or json.load(open(pj, encoding="utf-8"))["changesets"] == []
    arc_b = _ok(a.ara_archive(["mod-B"]))["archives"]["mod-B"]["archive"]
    assert arc_b is None or arc_b["changesets"] == []
    # 形の違うものはエラー
    assert a.ara_restore("mod-B", {"format": "x"})["ok"] is False


# ================================================================ DAW の操作と取り消しの履歴
def test_sync_moves_tracks_reopens_and_is_not_in_the_history(ara, tmp_path):
    m, a = ara
    from vocal_engine import mcp_tracks as mt
    take = _wav(tmp_path / "src" / "take.wav", _voice())
    guide = _wav(tmp_path / "src" / "guide.wav", _voice(transpose=0.3, seed=2))
    _open(a)
    t1 = _add(a, "take", take, offset_sec=4.0)["track"]["id"]
    tg = _add(a, "guide", guide, offset_sec=4.0)["track"]["id"]
    notes = _select_and_analyze(m, t1)
    _ok(mt.set_guide_track(tg))                  # 画面の操作（取り消せる）
    _ok(m.shift_pitch(100, note_id=notes[0]))
    size = mt.history_summary()["size"]
    s = m._state["session"]
    # 位置が変わらない（1 サンプル未満）なら何もしない
    r = _ok(a.ara_sync([{"ara_id": "guide", "offset_sec": 4.0 + 0.1 / SR}]))
    assert r["changed"] == [] and r["reopened"] is False
    # ガイドを動かす: 編集対象のガイドの重ね方が変わるので開き直す
    r = _ok(a.ara_sync([{"ara_id": "guide", "offset_sec": 4.5, "name": "Guide", "group": "Ref"},
                        {"ara_id": "nope", "offset_sec": 1.0}],
                       tempo={"bpm": 128, "numerator": 3, "denominator": 4, "start_sec": 0.25}))
    assert r["changed"] == [tg] and r["reopened"] is True and r["unknown"] == ["nope"]
    assert r["tempo"]["bpm"] == 128 and r["tempo"]["num"] == 3 and r["tempo"]["source"] == "daw"
    assert s.track(tg)["offset_sec"] == 4.5 and s.track(tg)["name"] == "Guide"
    assert mt.history_summary()["size"] == size      # 履歴に入らない
    # undo は画面の編集（ピッチ）を戻し、DAW の位置・テンポは戻さない
    u = _ok(m.undo())
    assert u["undone"]["label"] == "ピッチ"
    assert s.track(tg)["offset_sec"] == 4.5 and s.tempo["bpm"] == 128
    # 次の undo はガイドの指定（画面の操作）。DAW の位置・名前・テンポはそのまま
    u = _ok(m.undo())
    assert u["undone"]["label"] == "ガイドの指定" and s.guide is None
    assert s.track(tg)["offset_sec"] == 4.5 and s.track(tg)["name"] == "Guide" and s.tempo["bpm"] == 128
    _ok(m.redo())
    assert s.guide == tg and s.track(tg)["offset_sec"] == 4.5
    # アーカイブのガイドを戻す（履歴に入れない）: ara_id で指定・"" で外す・省けば今のまま
    _ok(mt.select_track(t1))                     # redo でガイドのトラックに切り替わっている
    r = _ok(a.ara_sync(guide=""))
    assert r["guide"] is None and r["guide_changed"] and r["reopened"] and s.guide is None
    r = _ok(a.ara_sync(guide="guide"))
    assert r["guide"] == tg and r["guide_changed"] and r["reopened"]
    assert _ok(a.ara_sync())["guide"] == tg
    assert _ok(a.ara_sync(guide="nope"))["unknown"] == ["nope"] and s.guide == tg
    assert mt.history_summary()["size"] == size
    assert _ok(a.ara_archive())["guide"] == "guide"


def test_undo_after_ara_ops_does_not_undo_daw_operations(ara, tmp_path):
    m, a = ara
    from vocal_engine import mcp_tracks as mt
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    src2 = _wav(tmp_path / "src" / "b.wav", _voice(transpose=2, seed=1))
    _open(a)
    t1 = _add(a, "mod-A", src)["track"]["id"]
    tb = _add(a, "mod-B", src2)["track"]["id"]
    _ok(mt.set_guide_track(tb))                  # 画面の操作（session の項目）
    notes = _select_and_analyze(m, t1)
    _ok(m.shift_pitch(100, note_id=notes[0]))
    # DAW: 修飾を足す・外す・名前を変える
    _add(a, "mod-C", src2, name="C")
    _ok(a.ara_set_modification("mod-A", src, name="A2", offset_sec=2.0))
    _ok(a.ara_remove_modification("mod-B"))
    s = m._state["session"]
    ids = [t["id"] for t in s.tracks]
    assert tb not in ids and s.guide is None      # ガイドが外れた
    u = _ok(m.undo())                             # 画面のピッチ
    assert u["undone"]["label"] == "ピッチ" and u["total_edits"] == 0
    u = _ok(m.undo())                             # 画面のガイドの指定。外した mod-B は戻らない
    assert u["undone"]["label"] == "ガイドの指定"
    assert [t["id"] for t in s.tracks] == ids and s.track(t1)["name"] == "A2"
    assert s.track(t1)["offset_sec"] == 2.0
    assert m.undo()["ok"] is False                # それより前は無い（DAW の操作は入っていない）


def test_remove_and_add_back_keeps_the_id_and_edits(ara, tmp_path):
    m, a = ara
    from vocal_engine import mcp_tracks as mt
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    src2 = _wav(tmp_path / "src" / "b.wav", _voice(transpose=2, seed=1))
    _open(a)
    ta = _add(a, "mod-A", src)["track"]["id"]
    tb = _add(a, "mod-B", src2)["track"]["id"]
    notes = _select_and_analyze(m, ta)
    _ok(m.shift_pitch(100, note_id=notes[0]))
    pdir = m._state["session"].project_dir_of(m._state["session"].find_ara("mod-A"))
    r = _ok(a.ara_remove_modification("mod-A"))
    assert r["removed"] and r["switched_to"] == tb
    assert os.path.exists(os.path.join(pdir, "project.json"))
    assert m.undo()["ok"] is False                # 外したトラックの編集は Ctrl+Z の列から外す
    assert _ok(a.ara_remove_modification("mod-A"))["removed"] is False
    # 最後の 1 本も外せる（ボーカルが 0 本）
    r = _ok(a.ara_remove_modification("mod-B"))
    assert r["switched_to"] is None and m._state["project"] is None and mt.current_track_id() is None
    # DAW の取り消しで戻ってきた: 同じ id・同じ編集。編集対象が無いので編集対象になる
    r = _add(a, "mod-A", src)
    assert r["created"] and r["track"]["id"] == ta and r["selected"] and r["source_changed"] is False
    assert len(m._state["project"].edits) == 1


def test_rewritten_source_keeps_edits_and_changed_audio_is_reported(ara, tmp_path):
    """プラグインがソースの WAV を書き直す（同じ音・別の書式）→ 編集はそのまま。音が変わった → source_changed。"""
    m, a = ara
    x = _voice()
    src = _wav(tmp_path / "src" / "a.wav", x)
    _open(a)
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)
    _ok(m.shift_pitch(100, note_id=notes[0]))
    time.sleep(0.02)
    _wav(src, sf.read(src, dtype="float64")[0], subtype="DOUBLE")    # 同じ音・別のバイト
    # ara_set_modification の前に開こうとしても、編集を捨てずに断る
    s = m._state["session"]
    with pytest.raises(Exception):
        s.open_project_for(s.find_ara("mod-A"))
    r = _add(a, "mod-A", src)
    assert r["created"] is False and r["source_changed"] is False and r["selected"]
    assert len(m._state["project"].edits) == 1
    c = Cache(src)
    c.sync(a, "mod-A")
    _same(c, a, "mod-A")
    # 前の作業場所をエンジンの起動し直しで開く（ara_open の前に書き直されていた）
    from vocal_engine import mcp_document as md
    md._clear()
    time.sleep(0.02)
    _wav(src, x)
    r = _open(a)
    assert r["project_dir"] and len(m._state["project"].edits) == 1
    # 音が変わった（DAW で差し替えた）: 編集は残し、知らせる
    time.sleep(0.02)
    _wav(src, _voice(gain=0.2))
    r = _add(a, "mod-A", src)
    assert r["source_changed"] is True and len(m._state["project"].edits) == 1


def test_clone_of_copies_the_edits(ara, tmp_path):
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    other = _wav(tmp_path / "src" / "b.wav", _voice(transpose=1))
    _open(a)
    ta = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, ta)
    _ok(m.shift_pitch(100, note_id=notes[0]))
    r = _add(a, "mod-A2", src, clone_of="mod-A")
    assert r["cloned"] is True
    arcs = _ok(a.ara_archive())["archives"]
    assert arcs["mod-A2"]["archive"]["changesets"] == arcs["mod-A"]["archive"]["changesets"]
    c1, c2 = Cache(src), Cache(src)
    c1.sync(a, "mod-A")
    r2 = c2.sync(a, "mod-A2")
    assert r2["analysis_pending"] is True or not _prep_off()      # 複製はまだ解析していない（裏の準備がする）
    _select_and_analyze(m, r["track"]["id"])
    c2.sync(a, "mod-A2")
    assert np.array_equal(c1.buf, c2.buf)
    assert _add(a, "mod-X", other, clone_of="mod-A")["cloned"] is False   # 素材が違う


# ================================================================ ロック
def test_archive_and_revs_do_not_wait_for_the_engine_lock(ara, tmp_path):
    """解析のジョブなどがエンジンのロック（_lock）を握っている間も、保存（ara_archive）と版の確認は止まらない。"""
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    _open(a)
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)
    _ok(m.shift_pitch(100, note_id=notes[0]))
    held, release = threading.Event(), threading.Event()

    def hold():
        with m._lock:
            held.set()
            release.wait(10)

    th = threading.Thread(target=hold, daemon=True)
    th.start()
    assert held.wait(5)
    try:
        out = {}

        def call():
            out["archive"] = a.ara_archive()
            out["revs"] = a.ara_revs()

        c = threading.Thread(target=call, daemon=True)
        t0 = time.perf_counter()
        c.start()
        c.join(3.0)
        assert not c.is_alive(), "ロックを待っている"
        assert time.perf_counter() - t0 < 3.0
        assert out["archive"]["ok"] and out["archive"]["archives"]["mod-A"]["archive"]["changesets"]
        assert out["revs"]["ok"] and ":" in out["revs"]["revs"]["mod-A"]
    finally:
        release.set()
        th.join(5)


# ================================================================ 細かいところ
def test_bridge_ara_client_and_tool_permissions(monkeypatch):
    from vocal_engine import bridge
    monkeypatch.setenv(bridge.CLIENT_ENV, "ara")
    assert bridge.is_app() and bridge.is_ara()
    assert bridge.denied("ara_open") is None and bridge.denied("export_wav") is None
    monkeypatch.setenv(bridge.CLIENT_ENV, "")
    assert not bridge.is_app()
    r = bridge.denied("ara_render_dirty")
    assert r["ok"] is False and r["permission"] == "ara"


def test_ara_document_kind(tmp_path, monkeypatch):
    from vocal_engine.project import document as D
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))
    wd = D.ara_work_dir("8c4f-01_x")
    assert wd == os.path.join(str(tmp_path / "work"), "ara", "8c4f-01_x")
    for bad in ("", "a/b", "..", "a" * 65, "曲"):
        with pytest.raises(D.DocumentError):
            D.ara_work_dir(bad)
    os.makedirs(wd)
    d = D.Document("ara", wd, name="x")
    assert d.dirty() is False and d.info()["kind"] == "ara"
    D.write_marker(wd, kind="ara", name="x")
    with pytest.raises(D.DocumentError):
        D.classify(wd)


# ================================================================ stdio
@pytest.mark.skipif(not os.path.exists(VENV_PY), reason=".venv の python が無い")
async def test_ara_over_stdio(tmp_path):
    from mcp import StdioServerParameters
    from mcp.client import Client
    from test_mcp_stdio import payload
    src = _wav(tmp_path / "src" / "a.wav", _voice(stereo=True))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", GLISS_CLIENT="ara", GLISS_F0_ESTIMATOR="praat",
               VOCAL_ENGINE_WORK_DIR=str(tmp_path / "work"), VOCAL_ENGINE_AUTO_LYRICS="0",
               VOCAL_ENGINE_PREP="0")
    engine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    params = StdioServerParameters(command=VENV_PY, args=["-m", "vocal_engine.mcp"], cwd=engine, env=env)

    async def call(c, name, args=None):
        r = payload(await c.call_tool(name, args or {}))
        assert r.get("ok") is not False, (name, r)
        return r

    async with Client(params, read_timeout_seconds=120) as c:
        names = [t.name for t in (await c.list_tools()).tools]
        for want in ("ara_open", "ara_set_modification", "ara_remove_modification", "ara_sync",
                     "ara_render_dirty", "ara_revs", "ara_archive", "ara_restore"):
            assert want in names
        r = await call(c, "ara_open", {"work_key": "stdio-doc", "name": "曲"})
        assert r["document"]["kind"] == "ara"
        r = await call(c, "ara_set_modification", {"ara_id": "mod-A", "source_path": src, "source_id": "src-1",
                                                   "name": "Vox", "offset_sec": 1.5})
        assert r["selected"]
        await call(c, "analyze_take", {"background": False})
        notes = (await call(c, "list_notes", {"kind": "note"}))["notes"]
        await call(c, "shift_pitch", {"cents": 120, "note_id": notes[1]["id"]})
        revs = (await call(c, "ara_revs"))["revs"]
        cache = Cache(src)
        while True:
            r = await call(c, "ara_render_dirty", {"ara_id": "mod-A", "since": cache.rev})
            cache.apply(r)
            if not r["more"]:
                break
        assert cache.rev == revs["mod-A"] and len(r["windows"]) == 1
        assert not np.array_equal(cache.buf, cache.orig)
        arc = (await call(c, "ara_archive"))["archives"]["mod-A"]["archive"]
        assert len(arc["changesets"]) == 1
        r = await call(c, "ara_sync", {"tracks": [{"ara_id": "mod-A", "offset_sec": 2.0}]})
        assert r["changed"]
        r = await call(c, "ara_restore", {"ara_id": "mod-A", "archive": arc})
        assert r["mismatch"] is False
        assert (await call(c, "project_status"))["document"]["dirty"] is False
        await call(c, "ara_remove_modification", {"ara_id": "mod-A"})
