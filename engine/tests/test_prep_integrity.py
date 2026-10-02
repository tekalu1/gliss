# -*- coding: utf-8 -*-
"""裏の準備（issue #63）における整合性とプロセス間排他のテスト。

1. 別のプロセスのエンジンと同時に保存しても、書きかけのファイルを取り違えず、競合時は古い編集を拒否する
2. 表が解析を要するとき、順番待ちの準備に合流する（表と裏で 2 回計算しない）
3. 作業場所を消した後に、裏の準備が古い project.json を書き戻さない
4. 必要なキャッシュが欠けていたら「準備済み」の印を書かない。対応付けに失敗した組は「失敗」
5. 歌詞を変えたら準備済みでなくなり、音素を裏で準備し直す
6. 別のプロセスとの準備の取り合いは OS のロックで決める。状態の印の時刻は長い段の途中でも更新する
7. SHA-256・音声ファイルの情報は、トラックごとのロックの外で取る

別のプロセスは multiprocessing（spawn）で立てる。解析は test_prep.py の偽物を使う。
"""
import hashlib
import json
import multiprocessing as mp
import os
import threading
import time

import pytest

from test_prep import FREQ, _ok, _open, _ready, _state, _until, env  # noqa: F401  env は fixture

SPAWN = mp.get_context("spawn")


@pytest.fixture(autouse=True)
def _unpause():
    yield
    from vocal_engine import prep
    prep.set_paused(False)                       # 途中で落ちても次のテストに一時停止を残さない


# ---------------------------------------------------------------- 別のプロセスで動かすもの
def _child_save(pdir, label, a_in, b_wrote, a_done, out):
    """A は project.json を置き換える途中で止まり、その間に B が保存する。"""
    from vocal_engine.project import store
    orig = store.replace_file

    def hook(tmp, dst, *a, **k):
        if os.path.basename(dst) == "project.json":
            if label == "A":
                a_in.set()
                b_wrote.wait(2.0)                # 直した後は B がロックで待つので来ない（時間切れで進む）
                orig(tmp, dst, *a, **k)
                a_done.set()
                return None
            b_wrote.set()
            a_done.wait(10.0)
        return orig(tmp, dst, *a, **k)
    store.replace_file = hook
    if label == "B":
        a_in.wait(20)
    p = store.Project(pdir)
    p.analysis = {"writer": label}
    try:
        p.save()
        out.put((label, "ok"))
    except Exception as e:                       # noqa: BLE001
        out.put((label, "%s: %s" % (type(e).__name__, e)))


def _child_lyrics(pdir, label, text, span, a_paused, b_saved, out):
    """A は読んでから保存するまでの間に止まり、その間に B が別の区間の歌詞を入れて保存する。"""
    from vocal_engine.project import store
    p = store.Project(pdir).load()
    if label == "A":
        orig = store.Project.save

        def save(self):
            a_paused.set()
            b_saved.wait(20)
            return orig(self)
        store.Project.save = save
    else:
        a_paused.wait(20)
    try:
        p.set_lyrics(text, start_sec=span[0], end_sec=span[1])
        out.put((label, "ok"))
    except Exception as e:                       # noqa: BLE001
        out.put((label, "%s: %s" % (type(e).__name__, e)))
    finally:
        if label == "B":
            b_saved.set()


def _child_hold_lock(path, held, release):
    """別のプロセスのエンジンが準備している（prep.lock の先頭 1 バイトを OS のロックで押さえる）。"""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    held.set()
    release.wait(30)
    os.close(fd)


def _results(out, n=2):
    return dict(out.get(timeout=30) for _ in range(n))


def _new_project(tmp_path, name="p"):
    from test_prep import _audio
    from vocal_engine.project.store import Project
    take = _audio(tmp_path, "take")
    pdir = str(tmp_path / name)
    Project.open(take, project_dir=pdir, set_log=False, memo=False)
    return pdir


# ---------------------------------------------------------------- 1. 別のプロセスとの同時保存
def test_cross_process_save_does_not_share_tmp(tmp_path):
    """同じ一時ファイル名を使うと、A が B の中身を置いて成功を返し、B は一時ファイルが消えて失敗していた。"""
    pdir = _new_project(tmp_path)
    a_in, b_wrote, a_done = SPAWN.Event(), SPAWN.Event(), SPAWN.Event()
    out = SPAWN.Queue()
    ps = [SPAWN.Process(target=_child_save, args=(pdir, x, a_in, b_wrote, a_done, out)) for x in "AB"]
    for p in ps:
        p.start()
    res = _results(out)
    for p in ps:
        p.join(30)
    assert res == {"A": "ok", "B": "ok"}, res
    with open(os.path.join(pdir, "project.json"), encoding="utf-8") as f:
        assert json.load(f)["analysis"]["writer"] == "B"      # 後から保存した方（A の保存の後に B）
    assert not [n for n in os.listdir(pdir) if n.endswith(".tmp")]


def test_cross_process_edit_reports_conflict(tmp_path):
    """別プロセスの保存後は古い引数を自動適用せず競合を返す。"""
    pdir = _new_project(tmp_path)
    a_paused, b_saved = SPAWN.Event(), SPAWN.Event()
    out = SPAWN.Queue()
    ps = [SPAWN.Process(target=_child_lyrics, args=(pdir, "A", "あ", (0.1, 0.5), a_paused, b_saved, out)),
          SPAWN.Process(target=_child_lyrics, args=(pdir, "B", "い", (1.0, 1.5), a_paused, b_saved, out))]
    for p in ps:
        p.start()
    res = _results(out)
    for p in ps:
        p.join(30)
    assert res["B"] == "ok" and res["A"].startswith("ProjectConflict:"), res
    from vocal_engine.project.store import Project
    texts = sorted(e["text"] for e in Project(pdir).load().lyrics_entries("take"))
    assert texts == ["い"]


def test_save_conflict_in_same_process_reports_edit(tmp_path):
    """別インスタンスが間に書いても古い計算結果を適用しない。"""
    from vocal_engine.project.store import Project, ProjectConflict
    from vocal_engine.project.model import Target
    pdir = _new_project(tmp_path)
    a = Project(pdir).load()
    b = Project(pdir).load()
    b.set_lyrics("い", start_sec=1.0, end_sec=1.5)
    # a はまだ読み直していない（reload_if_changed を呼ばない change_lyrics 以外の経路: apply_changes）
    time.sleep(0.01)
    with pytest.raises(ProjectConflict):
        a.apply_changes([], [{"kind": "pitch_shift", "target": Target.range(0.2, 0.6),
                              "params": {"cents": 25.0}}])
    d = Project(pdir).load()
    assert len(d.edits) == 0 and [e["text"] for e in d.lyrics_entries("take")] == ["い"]


# ---------------------------------------------------------------- 2. 順番待ちの準備への合流
def test_front_joins_queued_prep_instead_of_computing(env, monkeypatch):
    """開いた直後（準備が順番待ち）に解析を要するツールを呼ぶと、準備を最優先にして待つ（表は計算しない）。"""
    m, mt, prep, fake, paths, sdir = env
    who = []
    est = fake.estimate

    def estimate(x, sr, **k):
        who.append(threading.current_thread().name)
        return est(x, sr, **k)
    from vocal_engine.project import store
    monkeypatch.setattr(store, "estimate_f0", estimate)
    gate = fake.gate[("f0", FREQ["take"])] = threading.Event()
    _ok(m.pause_prep(True))                      # 準備は順番待ちのまま（合流されたものだけ進む）
    r = _open(m, paths, sdir)
    t1 = r["session"]["current"]
    assert _state(prep, t1)["state"] == "queued"
    res = {}
    th = threading.Thread(target=lambda: res.update(r=m.list_notes()))
    th.start()
    _until(lambda: who)
    time.sleep(0.2)
    assert who == ["gliss-prep"]                 # 計算は裏の 1 回だけ（表は合流して待っている）
    gate.set()
    th.join(15)
    assert res["r"]["ok"] and res["r"]["notes"]
    assert fake.f0_count("take") == 1
    _ok(m.pause_prep(False))
    _until(_ready(prep, t1))
    assert fake.f0_count("take") == 1


def test_front_wait_ends_on_reset(env):
    """合流して待っている間に曲を閉じた（reset）: 待つのをやめる。取り消された準備が済ませた段は使う。"""
    m, mt, prep, fake, paths, sdir = env
    gate = fake.gate[("f0", FREQ["take"])] = threading.Event()
    _ok(m.pause_prep(True))
    _open(m, paths, sdir)
    res = {}
    th = threading.Thread(target=lambda: res.update(r=m.list_notes()))
    th.start()
    _until(lambda: fake.entered.get(("f0", FREQ["take"])))
    prep.reset()
    gate.set()
    th.join(15)
    assert not th.is_alive() and res["r"]["ok"]
    assert fake.f0_count("take") == 1            # 裏が F0 の段を済ませて止まった: 表はそれを読む
    _ok(m.pause_prep(False))


def test_front_claims_track_while_computing(env):
    """表が自分で計算している間（準備が失敗していた）、裏は同じトラックを始めない。"""
    m, mt, prep, fake, paths, sdir = env
    fake.fail.add(("f0", FREQ["take"]))
    r = _open(m, paths, sdir)
    t1 = r["session"]["current"]
    _until(lambda: (_state(prep, t1) or {}).get("state") == "failed")
    fake.fail.clear()
    fake.entered.pop(("f0", FREQ["take"]), None)
    gate = fake.gate[("f0", FREQ["take"])] = threading.Event()
    res = {}
    th = threading.Thread(target=lambda: res.update(r=m.list_notes()))
    th.start()
    _until(lambda: fake.entered.get(("f0", FREQ["take"])))
    with prep.PREPARER._cv:                      # 表の計算の間に、準備を入れ直しても
        prep.PREPARER._items[t1].state = "queued"
        prep.PREPARER._cv.notify_all()
    time.sleep(0.3)
    assert prep.PREPARER._running is None        # 占有されているので始めない
    gate.set()
    th.join(15)
    assert res["r"]["ok"]
    _until(_ready(prep, t1))
    assert fake.f0_count("take") == 2            # 裏の失敗 1・表 1。表の後の準備はキャッシュを読む


# ---------------------------------------------------------------- 3. 作業場所を消した後の書き戻し
def test_closing_while_commit_is_stuck_does_not_resurrect(env, monkeypatch):
    m, mt, prep, fake, paths, sdir = env
    from vocal_engine import mcp_document as md
    from vocal_engine.project import store
    entered, release = threading.Event(), threading.Event()
    orig = store.Project.merge_background

    def slow(self, q, *a, **k):
        entered.set()
        assert release.wait(20)
        return orig(self, q, *a, **k)
    monkeypatch.setattr(store.Project, "merge_background", slow)
    _ok(md.new_project(take_path=paths["take"]))
    wd = md.current().work_dir
    assert entered.wait(15)
    # ワーカーは project.json を読んだ後で止まっている。離れるのを待たない（時間切れ）で閉じる
    monkeypatch.setattr(store, "PREP_RELEASE", lambda root, **k: prep.PREPARER.release_tree(root, timeout=0))
    t0 = time.perf_counter()
    _ok(md.close_project(discard=True))
    assert time.perf_counter() - t0 < 2.0        # エンジンのロックを握ったまま待たない
    release.set()
    _until(lambda: prep.PREPARER._running is None)
    _until(lambda: not os.path.exists(wd))       # 離れてから消す
    time.sleep(0.3)
    assert not os.path.exists(wd)                # 古い project.json を書き戻していない
    assert prep.PREPARER.history[-1]["outcome"] == "cancel"


def test_reset_work_fails_when_prep_does_not_leave(tmp_path, monkeypatch):
    from vocal_engine.project import document as D
    from vocal_engine.project import store
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "session.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(store, "PREP_RELEASE", lambda root, **k: False)
    with pytest.raises(D.DocumentError):
        D.reset_work(str(wd))
    assert (wd / "session.json").exists()


def test_background_project_does_not_recreate_removed_dir(tmp_path):
    from vocal_engine.project.store import Project, ProjectError
    import shutil
    pdir = _new_project(tmp_path)
    q = Project(pdir).load()
    q.background = True
    shutil.rmtree(pdir)
    with pytest.raises(ProjectError):
        q.save()
    with pytest.raises(ProjectError):
        q._cache_path("take-analysis.json")
    assert not os.path.exists(pdir)


# ---------------------------------------------------------------- 4. 欠けたキャッシュ・対応付けの失敗
def test_stamp_requires_guide_caches(tmp_path):
    from vocal_engine import prep

    class FakeProject:
        def __init__(self, pdir):
            self.dir = str(pdir)
            self.guide = {"path": "guide.wav"}
            self._guide_cache_path = str(pdir / "cache" / "guide" / "missing")
            self._srcs = {}

        def _alignment_cache_dir(self):
            return str(tmp_path / "stamp" / "cache" / "guide" / "alignment" / "missing")

        def lyrics_entries(self, role):
            return []

        def phoneme_error(self, role):
            return None

    pdir = tmp_path / "stamp"
    (pdir / "cache").mkdir(parents=True)
    (pdir / "cache" / "take-analysis.json").write_text("{}", encoding="utf-8")
    try:
        prep.write_stamp(FakeProject(pdir), "sig")
    except prep.Incomplete:
        pass
    assert not prep.stamp_ok(str(pdir), "sig")


def test_stamp_notices_changed_cache(env):
    """印に書いたキャッシュが変わった・消えたら準備済みではない。"""
    m, mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir, guide="g1")
    t1 = r["session"]["current"]
    _until(_ready(prep, t1))
    s = m._state["session"]
    t = s.track(t1)
    pdir = s.project_dir_of(t)
    sig = prep.track_sig(s, t)
    assert prep.stamp_ok(pdir, sig)
    [e] = [e for e in prep._stamp_entries(pdir) if e["sig"] == sig]
    names = [os.path.basename(f[0]) for f in e["files"]]
    assert {"take-analysis.json", "guide-analysis.json", "alignment.json", "onsets.json",
            "onsets-take.json"} <= set(names)
    align = [f[0] for f in e["files"] if f[0].endswith("alignment.json")][0]
    with open(align, "a", encoding="utf-8") as f:
        f.write(" ")
    assert not prep.stamp_ok(pdir, sig)
    os.remove(align)
    assert not prep.stamp_ok(pdir, sig)


def test_alignment_fallback_is_failed_not_ready(env, monkeypatch):
    """DTW が失敗して位置のままの対応（#32）: 準備済みにせず「失敗」。同じ組み合わせでは裏でやり直さない。"""
    m, mt, prep, fake, paths, sdir = env
    from vocal_engine.project import align_helper
    calls = []

    def bad(project, method):
        calls.append(threading.current_thread().name)
        raise RuntimeError("偽の DTW の失敗")
    r = _open(m, paths, sdir, guide="g1")
    t1 = r["session"]["current"]
    _until(_ready(prep, t1))
    monkeypatch.setattr(align_helper, "compute_alignment", bad)
    tb = _ok(mt.add_track(paths["b"]))["track"]
    st = _until(lambda: (lambda x: x if x and x["state"] == "failed" else None)(_state(prep, tb)))
    assert "対応付け" in st["error"] and "偽の DTW の失敗" in st["error"]
    assert len(calls) == 1
    # 開き直しても失敗のまま（やり直さない）
    prep.reset()
    m._state.update(project=None, session=None, track=None)
    _open(m, paths, sdir)
    assert _state(prep, tb)["state"] == "failed"
    time.sleep(0.3)
    assert len(calls) == 1
    # 選ぶと表で取り直す（#32 と同じ。裏の準備にはやり直させないので 1 回だけ）
    _ok(mt.select_track(tb))
    a = _ok(m.analyze_take())
    assert "guide_warning" in a
    assert len(calls) == 2
    assert _state(prep, tb)["state"] == "failed"


# ---------------------------------------------------------------- 5. 歌詞を変えたら音素を準備し直す
def test_lyrics_change_reprepares_phonemes(env, monkeypatch):
    m, mt, prep, fake, paths, sdir = env
    from vocal_engine.phoneme import analyze as PA
    from vocal_engine.phoneme.model import PhonemeResult
    ph = []
    ph_gate = threading.Event()

    def align(x, sr, entries, f0r=None, source="take", duration_sec=0.0, **k):
        ph.append((threading.current_thread().name, [e["text"] for e in entries]))
        assert ph_gate.wait(20)
        text = "".join(e["text"] for e in entries)
        return PhonemeResult(source=source, lyrics=text, kana=text, phonemes=[], boundaries=[],
                             entries=list(entries), duration_sec=duration_sec)
    monkeypatch.setattr(PA, "align_lyrics_ranges", align)
    r = _open(m, paths, sdir)
    t1 = r["session"]["current"]
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(_ready(prep, t1, tb))
    _ok(mt.select_track(tb))
    _ok(m.set_lyrics("あ", start_sec=0.1, end_sec=0.5, reanalyze=False))
    _ok(mt.select_track(t1))                     # b はもう開いていない
    s = m._state["session"]
    assert not prep.is_ready(s, s.track(tb))     # 歌詞が変わった: 準備済みではない
    _until(lambda: ph)
    assert ph == [("gliss-prep", ["あ"])]          # 音素を裏で準備し直している
    ph_gate.set()
    _until(_ready(prep, tb))
    assert prep.is_ready(s, s.track(tb))
    n = (len(fake.calls["f0"]), len(fake.calls["dtw"]), len(ph))
    r = _ok(mt.select_track(tb))
    assert r["analyzed"] is True
    a = _ok(m.analyze_take())                    # キャッシュを読むだけ（表で音素を計算しない）
    assert a["phonemes"]["has_lyrics"] is True
    assert (len(fake.calls["f0"]), len(fake.calls["dtw"]), len(ph)) == n


def test_lyrics_change_by_other_engine_is_noticed(env, monkeypatch):
    """別のエンジンが歌詞を変えた（project.json を直接書いた）: 開いたとき・確かめたときに準備済みでない。"""
    m, mt, prep, fake, paths, sdir = env
    from vocal_engine.phoneme import analyze as PA
    from vocal_engine.phoneme.model import PhonemeResult

    def align(x, sr, entries, f0r=None, source="take", duration_sec=0.0, **_kw):
        text = "".join(e["text"] for e in entries)
        return PhonemeResult(source=source, lyrics=text, kana=text, phonemes=[], boundaries=[],
                             entries=list(entries), duration_sec=duration_sec)

    monkeypatch.setattr(PA, "align_lyrics_ranges", align)
    r = _open(m, paths, sdir)
    t1 = r["session"]["current"]
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(_ready(prep, t1, tb))
    s = m._state["session"]
    from vocal_engine.project.store import Project
    ext = Project(s.project_dir_of(s.track(tb))).load()
    time.sleep(0.02)
    ext.set_lyrics("い", start_sec=1.0, end_sec=1.5)
    assert not prep.is_ready(s, s.track(tb))
    _until(_ready(prep, tb))
    assert prep.is_ready(s, s.track(tb))


# ---------------------------------------------------------------- 6. 別のプロセスとの準備の取り合い
def test_other_process_holding_prep_lock_defers(env, monkeypatch):
    """別のプロセスが prep.lock を握っている間は、印（preparing.json）が無くても・古くても始めない。"""
    m, mt, prep, fake, paths, sdir = env
    monkeypatch.setattr(prep, "DEFER_SEC", 0.2)
    _open(m, paths, sdir)
    _ok(m.analyze_take())
    _ok(m.pause_prep(True))
    r = _ok(mt.add_track(paths["b"]))
    tb = r["track"]
    bdir = [t["project_dir"] for t in r["session"]["tracks"] if t["id"] == tb][0]
    os.makedirs(os.path.join(bdir, "cache"), exist_ok=True)
    with open(os.path.join(bdir, "cache", prep.RUNNING_FILE), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid() + 100000, "at": time.time() - 3600}, f)   # 古い印
    held, release = SPAWN.Event(), SPAWN.Event()
    child = SPAWN.Process(target=_child_hold_lock, args=(os.path.join(bdir, "prep.lock"), held, release))
    child.start()
    try:
        assert held.wait(30)
        _ok(m.pause_prep(False))
        time.sleep(0.8)
        assert _state(prep, tb)["state"] == "queued" and fake.f0_count("b") == 0
    finally:
        release.set()
        child.join(30)
    _until(_ready(prep, tb))                     # 落ちた・終わったプロセスのロックは OS が外す
    assert fake.f0_count("b") == 1
    assert not prep.foreign_running(bdir)


def test_running_marker_is_refreshed_during_long_stage(env, monkeypatch):
    m, mt, prep, fake, paths, sdir = env
    monkeypatch.setattr(prep, "HEARTBEAT_SEC", 0.05, raising=False)
    _open(m, paths, sdir)
    _ok(m.analyze_take())
    gate = fake.gate[("f0", FREQ["b"])] = threading.Event()
    r = _ok(mt.add_track(paths["b"]))
    tb = r["track"]
    bdir = [t["project_dir"] for t in r["session"]["tracks"] if t["id"] == tb][0]
    _until(lambda: fake.entered.get(("f0", FREQ["b"])))
    marker = os.path.join(bdir, "cache", prep.RUNNING_FILE)

    def at():
        with open(marker, encoding="utf-8") as f:
            return json.load(f)["at"]
    a0 = _until(lambda: os.path.exists(marker) and at())
    time.sleep(0.4)                              # F0 の段の途中
    assert at() > a0
    gate.set()
    _until(_ready(prep, tb))
    assert not os.path.exists(marker)


# ---------------------------------------------------------------- 7. ロックの中で重い読み込みをしない
def test_hash_and_audio_info_outside_dir_lock(env, monkeypatch):
    m, mt, prep, fake, paths, sdir = env
    from vocal_engine import audio, media
    from vocal_engine.project import store
    _open(m, paths, sdir)
    _ok(m.pause_prep(True))
    tb = _ok(mt.add_track(paths["b"]))["track"]
    s = m._state["session"]
    t = s.track(tb)
    bad = []

    def held():
        for lk in list(store._dir_locks.values()):
            if (lk.held() if hasattr(lk, "held") else lk._is_owned()):
                return True
        return False

    class H:
        @staticmethod
        def sha256(*a):
            if held():
                bad.append("sha256")
            return hashlib.sha256(*a)
    monkeypatch.setattr(audio, "hashlib", H)
    real_info = media.sf.info

    def info(path, *a, **k):
        if held():
            bad.append("sf.info")
        return real_info(path, *a, **k)
    monkeypatch.setattr(media.sf, "info", info)
    for forget in (audio._sha_cache.clear, media._info_cache.clear):
        forget()
    s.prep_project_for(t)                        # 足したばかり（project.json が無い）: 作る
    for forget in (audio._sha_cache.clear, media._info_cache.clear):
        forget()
    s.open_project_for(t)                        # 表で開く
    assert bad == []
    _ok(m.pause_prep(False))
