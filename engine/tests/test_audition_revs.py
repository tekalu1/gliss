# -*- coding: utf-8 -*-
"""長押しの試聴が遅れる原因（docs/ara-plugin.md の「試聴の遅れ」）の確かめ。

- `ara_revs`（プラグインが 1 秒ごとに聞く全修飾の版）が、変わっていない project.json を毎回読み直さない
  （読み直しは Python の GIL を握り、同じエンジンの `render_audition` を秒単位で待たせた）。版・署名は読み直したときと同じ
- `render_audition`（cents = 0）が、ARA の再生用に作った同じ版の Segment 列を使い、層を作り直さない。音は変わらない
音は合成の歌声（重みの要らない Praat）。
"""
import os
import threading
import time

import numpy as np
import pytest
import soundfile as sf

from test_ara_tools import SR, Cache, _add, _ok, _open, _select_and_analyze, _voice, _wav, ara  # noqa: F401


def _load_counter(monkeypatch):
    from vocal_engine import mcp_ara
    calls = []
    real = mcp_ara.Project.load

    def counted(self, *a, **kw):
        calls.append(self.dir)
        return real(self, *a, **kw)

    monkeypatch.setattr(mcp_ara.Project, "load", counted)
    return calls


def _doc(m, a, tmp_path, n=3):
    """n 個の修飾（それぞれ編集を持つ）。"""
    _open(a)
    ids = []
    for i in range(n):
        src = _wav(tmp_path / "src" / ("v%d.wav" % i), _voice(seed=i))
        ara_id = "mod-%d" % i
        tid = _add(a, ara_id, src)["track"]["id"]
        notes = _select_and_analyze(m, tid)
        _ok(m.shift_pitch(100 + i, note_id=notes[0]))
        _ok(m.shift_pitch(-50, note_id=notes[1]))
        ids.append((ara_id, tid, notes))
    return ids


def test_ara_revs_rereads_only_what_changed(ara, tmp_path, monkeypatch):
    m, a = ara
    from vocal_engine import mcp_tracks as mt
    ids = _doc(m, a, tmp_path)
    monkeypatch.setattr(a, "SETTLE_NS", 0)           # 書いた直後でも信用する（この試験の時間を短くする）
    a._disk_cache.clear()
    calls = _load_counter(monkeypatch)

    first = _ok(a.ara_revs())
    assert len(calls) == len(ids)                    # 最初は全部読む
    for _ in range(5):
        again = _ok(a.ara_revs())
        assert again["revs"] == first["revs"] and again["states"] == first["states"]
    assert len(calls) == len(ids)                    # 変わっていなければ読み直さない

    # 読み直したときと同じ値（キャッシュは値を変えない）
    a._disk_cache.clear()
    fresh = _ok(a.ara_revs())
    assert fresh["revs"] == first["revs"] and fresh["states"] == first["states"]

    # 1 つの修飾の編集（project.json が変わる）: その修飾だけ読み直し、版と署名が変わる
    calls.clear()
    ara_id, tid, notes = ids[1]
    _ok(mt.select_track(tid))
    _ok(m.shift_pitch(77, note_id=notes[2]))
    changed = _ok(a.ara_revs())
    assert len(calls) == 1 and os.path.normcase(calls[0]).endswith(os.path.normcase(os.path.join("tracks", "ara-" + a._key(ara_id))))
    assert changed["revs"][ara_id] != first["revs"][ara_id]
    assert changed["states"][ara_id] != first["states"][ara_id]
    for other, _t, _n in ids:
        if other != ara_id:
            assert changed["revs"][other] == first["revs"][other]
    # 取り消し: 版が戻る
    _ok(m.undo())
    assert _ok(a.ara_revs())["revs"][ara_id] == first["revs"][ara_id]


def test_ara_revs_does_not_trust_a_file_written_just_now(ara, tmp_path, monkeypatch):
    m, a = ara
    ids = _doc(m, a, tmp_path, n=1)
    a._disk_cache.clear()
    calls = _load_counter(monkeypatch)
    for _ in range(3):
        _ok(a.ara_revs())
    assert len(calls) == 3                           # 書いてから 2 秒以内: 同じ時刻の次の書き込みを見逃さないよう毎回読む


def test_ara_revs_follows_the_estimator_of_the_track(ara, tmp_path, monkeypatch):
    m, a = ara
    ids = _doc(m, a, tmp_path, n=1)
    monkeypatch.setattr(a, "SETTLE_NS", 0)
    a._disk_cache.clear()
    before = _ok(a.ara_revs())
    s = m._state["session"]
    t = s.find_ara(ids[0][0])
    t["estimator"] = "praat" if t.get("estimator") != "praat" else "psola"   # 保存の署名は F0 の方式を含む
    after = _ok(a.ara_revs())
    assert after["states"][ids[0][0]] != before["states"][ids[0][0]]


def test_ara_revs_polling_does_not_slow_an_audition(ara, tmp_path, monkeypatch):
    """プラグインが版を聞き続けても、読み直しが起きないので、試聴を待たせない（読み直しは GIL を握る）。"""
    m, a = ara
    ids = _doc(m, a, tmp_path, n=6)
    monkeypatch.setattr(a, "SETTLE_NS", 0)
    a._disk_cache.clear()
    cold = time.perf_counter()
    _ok(a.ara_revs())
    cold = time.perf_counter() - cold
    warm = []
    for _ in range(10):
        t0 = time.perf_counter()
        _ok(a.ara_revs())
        warm.append(time.perf_counter() - t0)
    assert max(warm) < cold / 4, (cold, warm)         # 6 修飾の読み直し vs. stat だけ


def test_zero_cent_audition_uses_the_segments_of_the_ara_render(ara, tmp_path, monkeypatch):
    """窓の外の（編集の層だけが要る）範囲の試聴は、ARA の再生で作った Segment 列を使う。音は作り直したときと同じ。"""
    m, a = ara
    from vocal_engine.project import pitch
    src = _wav(tmp_path / "src" / "a.wav", _voice(stereo=True))
    _open(a)
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)
    _ok(m.shift_pitch(150, note_id=notes[0]))
    c = Cache(src)
    c.sync(a, "mod-A")
    last = notes[-1]                                  # 編集していないノート（窓の外）
    real = pitch.layered_segments
    built = []

    def spy(*args, **kw):
        built.append(1)
        return real(*args, **kw)

    monkeypatch.setattr(pitch, "layered_segments", spy)
    cached = _ok(m.render_audition(note_id=last))
    assert built == []                                # 層を作り直していない
    a._render.clear()                                 # ARA の再生の状態が無ければ、従来どおり作る
    plain = _ok(m.render_audition(note_id=last))
    assert built                                      # 作り直した
    x = sf.read(cached["path"], dtype="float32")[0]
    y = sf.read(plain["path"], dtype="float32")[0]
    np.testing.assert_array_equal(x, y)               # 音は同じ（ビット一致）
    # 動かした（cents ≠ 0）試聴は、動かした編集を足した層で作る（キャッシュの列は使わない）
    c.sync(a, "mod-A")
    built.clear()
    _ok(m.render_audition(note_id=last, cents=100))
    assert built


def test_stale_segments_are_not_used_after_an_edit(ara, tmp_path):
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    _open(a)
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)
    c = Cache(src)
    c.sync(a, "mod-A")
    p = m._state["project"]
    asig, erev = a._rev_parts(p)
    rev = "%s:%s" % (asig, erev)
    assert a.audition_segs("mod-A", p, "praat", asig, rev) is not None
    _ok(m.shift_pitch(100, note_id=notes[0]))         # 編集した後の版では、前の版の列を返さない
    asig2, erev2 = a._rev_parts(p)
    assert a.audition_segs("mod-A", p, "praat", asig2, "%s:%s" % (asig2, erev2)) is None


def test_audition_reuses_unchanged_windows_across_revisions(ara, tmp_path):
    """別の場所を編集して再合成しても、変わっていない窓の試聴は、前の版の PCM から返す（エンジンに作り直させない）。"""
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    _open(a)
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)
    _ok(m.shift_pitch(150, note_id=notes[0]))
    c = Cache(src)
    c.sync(a, "mod-A")
    assert _ok(m.render_audition(note_id=notes[0]))["timing_sec"]["ara_pcm_reused"]
    _ok(m.shift_pitch(-120, note_id=notes[4]))               # 遠い別のノート（別の窓）
    c.sync(a, "mod-A")
    first = _ok(m.render_audition(note_id=notes[0]))
    last = _ok(m.render_audition(note_id=notes[4]))
    assert first["timing_sec"]["ara_pcm_reused"], "前の版の窓が残っているはず"
    assert last["timing_sec"]["ara_pcm_reused"]
    ia = round(m._state["project"].note(notes[0]).start_sec * SR)
    ib = round(m._state["project"].note(notes[0]).end_sec * SR)
    got = sf.read(first["path"], dtype="float32")[0]
    np.testing.assert_array_equal(got, c.buf[ia:ib].mean(axis=1).astype("float32"))
    # 同じ窓を直し直したら、その窓は新しいファイルから
    _ok(m.shift_pitch(40, note_id=notes[0]))
    c.sync(a, "mod-A")
    again = _ok(m.render_audition(note_id=notes[0]))
    assert again["timing_sec"]["ara_pcm_reused"]
    got = sf.read(again["path"], dtype="float32")[0]
    np.testing.assert_array_equal(got, c.buf[ia:ib].mean(axis=1).astype("float32"))
