# -*- coding: utf-8 -*-
"""裏の準備（issue #63 の 2）: トラックの解析を、ユーザーが開く前に裏で済ませておく。

中身はトラックを選んだときの `analyze_take` と同じ（テイクの F0 と音符・歌詞の自動推定・音素・
セッションのガイドとの対応付け）。結果はトラックのプロジェクトのキャッシュ（鍵付きの保存）に置くので、
あとで選んだときの `analyze_take` はキャッシュを読むだけになる。

## スレッドとロック

- **ワーカーは 1 つ**（1 本ずつ進める）。エンジン全体の `_lock`（mcp_server.py）は**握らない**。
  重い計算の間も、表の編集・再生・保存のツールはそのまま動く。
- 対象のトラックは、表で開いている Project とは**別のインスタンス**で開く（`Session.prep_project_for`。
  `background=True`: project.json と、今の組み合わせを指す写しは書かず、鍵付きの保存だけに書く）。
- project.json に書くのは最後の 1 回だけ（`Project.merge_background`）。トラックごとのロック
  （`store.dir_lock`）を握り、**ディスクから読み直した最新の内容**に、テイク側の解析の要約と歌詞の
  自動推定を足して書く。表の保存（`Project.save`）・トラックを開く（`Session.open_project_for`）も
  同じロックを握るので、同じ project.json を同時に書かない。準備の間に表や外部（Claude Code）が
  入れた編集・歌詞は消さない。
- セッションは、準備を始めるたびに session.json から別に読む（表の Session オブジェクトには触らない）。

## 順番

今のトラック → ガイドのトラック（ほかのトラックがガイドの F0 をそのまま使える）→ 最近選んだ順 →
残りはトラックの並び順。段の境目（テイクの F0・歌詞の推定・ガイドの F0・対応付け・発音の頭・音素）で、
今のトラックが待っていれば譲る（済んだ段は鍵付きで保存してあるので、戻ったときに続きから）。

## 組み合わせ（sig）と取り消し

トラックごとに「テイクの素材・範囲 + セッションのガイドの切り出し + 方式」の署名を持つ。ガイドの指定・
位置・トラックの並びが変わったら `schedule` で署名を取り直し、変わったトラックの準備は取り消して
（走っていれば次の段の境目で止まる）新しい組み合わせで入れ直す。準備が済んだら
`cache/prepared.json` に署名を残す（エンジンを開き直しても、済んだトラックをやり直さない）。

## 合流

表の `analyze_take` は、そのトラックの準備が済んでいなければ、準備を最優先にして終わりを待ってから
（`join`）キャッシュを読むだけの解析をする。計算は 1 回で済む。待つ間はエンジンのロックを握らない
（ジョブのとき）。合流を取り消しても準備そのものは続く。`analyze_take` を呼ばずに解析の要るツール
（`list_notes` など）・音素が要るときも、順番待ち・準備中の準備に合流して待つ（`front`）。表が自分で計算する
ときは、その間トラックを占有して裏が同じ計算を始めないようにする。

## 準備済みの印

`cache/prepared.json` に、組み合わせの署名・必要なキャッシュのファイルと内容ハッシュ・テイクの歌詞の鍵を残す
（`write_stamp`）。欠けていれば書かない。後でファイルが変わった・歌詞が変わったら準備済みではない（音素を準備し直す）。
内容ハッシュは同じ版（更新時刻・サイズ・ID）の間は使い回す（`_cache_hash`）。同サイズ・同時刻の直接の上書きは
それでは見分けられないので、準備済みのキャッシュを実際に読んで失敗したら（`store.CacheBroken`）、そのファイルを
外し、印と使い回しを取り消して準備し直す（`broken`。表は合流して待つ）。
対応付けに失敗して位置のままの対応になった組み合わせ（#32）は「失敗」の印を書き、裏ではやり直さない。

## 別のプロセスのエンジン・作業場所の破棄

- 準備の間は `<プロジェクト>/prep.lock` の OS のロックを握る（`store.FileLock`）。取れなければほかのエンジンが
  準備しているので後回し。`cache/preparing.json` は状態を見る人向けの印で、HEARTBEAT_SEC ごとに時刻を更新する。
- project.json の読み書きは `store.dir_lock`（プロセスの中のロックと `project.lock` の OS のロック）の中。
- 作業場所を消す前は `release_tree` でやめさせて待つ（最長 RELEASE_WAIT_SEC 秒。過ぎたら `after_leave` で離れてから
  消す）。ワーカーは project.json・済みの印を書く直前（トラックのロックの中）にも取り消しを確かめ、消された
  ディレクトリを作り直さない。
"""
import contextlib
import hashlib
import json
import os
import threading
import time

from . import log
from . import media as M
from .analysis import f0 as F0
from .analysis.align import DEFAULT_METHOD as ALIGN_METHOD
from .phoneme import lyrics as LY
from .project import store as _store
from .project.session import Session
from .project.store import FileLock, Project, ProjectError, dir_lock, read_json, replace_file

PREP_VERSION = 1
STAMP_FILE = "prepared.json"
# 別のプロセスのエンジン（画面と Claude Code）が同じトラックを同時に準備しない: 準備の間
# `<プロジェクト>/prep.lock` の OS のロックを握る（`store.FileLock`。取れなければ後回し。落ちたプロセスのロックは
# OS が外す）。`cache/preparing.json`（pid と時刻）は状態を見る人向けの印で、準備の間 HEARTBEAT_SEC ごとに更新する
PREP_LOCK_FILE = "prep.lock"
RUNNING_FILE = "preparing.json"
HEARTBEAT_SEC = 5.0
DEFER_SEC = 5.0
LYRICS_DEFER_SEC = 1.0              # 歌詞を変えた後、音素を準備し直すまでの間（表が自分で取り直すなら譲る）
RELEASE_WAIT_SEC = 2.0               # 作業場所を消す前に、ワーカーが離れるのを待つ秒（過ぎたら離れてから消す）
FRONT_WAIT_SEC = 15.0                 # 同期ツールが応答を返すまでの上限（MCP の 20 秒ジョブ閾値より短い）


class PreparationPending(ProjectError):
    """裏の準備が続いている。呼び手は後で再試行できる。"""

# 段の名前（get_job / prep_status の stage）と画面に出す名前
STAGE_LABELS = {
    "open": "開いている",
    "take_f0": "テイクの音程",
    "lyrics": "歌詞の推定",
    "guide_f0": "ガイドの音程",
    "alignment": "ガイドとの対応付け",
    "onsets": "発音の頭",
    "phonemes": "音素",
    "commit": "書き込み",
    "view": "描画データ",                  # 済んだ後に、選んだときの描画データも作っておく（issue #63 の 3）
    "load": "読み込み",                   # 合流した analyze_take の最後（キャッシュを読んで開く）
}
_STAGE_PROGRESS = {"open": 0.0, "take_f0": 0.02, "lyrics": 0.45, "guide_f0": 0.5, "alignment": 0.65,
                   "onsets": 0.82, "phonemes": 0.9, "commit": 0.97, "view": 0.98}
# 準備の最後に描画データも作るか（VOCAL_ENGINE_PREP_VIEW=0 で作らない）
VIEW_ENABLED = os.environ.get("VOCAL_ENGINE_PREP_VIEW", "1") != "0"

# 外に見せる状態
READY, PREPARING, QUEUED, FAILED = "ready", "preparing", "queued", "failed"


def enabled():
    """VOCAL_ENGINE_PREP=0 で止める（エンジンの単体テストの既定。`tests/conftest.py`）。"""
    return os.environ.get("VOCAL_ENGINE_PREP", "1") != "0"


def _test_delay(path):
    """テスト用: 準備の段の頭ごとに待つ秒（`VOCAL_ENGINE_PREP_TEST_DELAY`。既定 0 = 待たない）。
    `VOCAL_ENGINE_PREP_TEST_MATCH` を渡すと、テイクのパスにその文字列を含むトラックだけ待つ。
    画面のテストが、準備中の印・準備に合流したときのポップアップを見るために使う（issue #63 の 4）。"""
    try:
        sec = float(os.environ.get("VOCAL_ENGINE_PREP_TEST_DELAY") or 0)
    except ValueError:
        return 0.0
    match = os.environ.get("VOCAL_ENGINE_PREP_TEST_MATCH")
    if sec <= 0 or (match and match.lower() not in str(path).lower()):
        return 0.0
    return sec


def _yield_enabled():
    """表の重い処理（再生前の音作り・書き出し）の間、次の段へ進まずに待つか。VOCAL_ENGINE_PREP_YIELD=0 で待たない。"""
    return os.environ.get("VOCAL_ENGINE_PREP_YIELD", "1") != "0"


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _under(key, p):
    """p が key（_norm 済み）そのものか、その下か。"""
    n = _norm(p)
    return n == key or n.startswith(key + os.sep)


class _Cancel(Exception):
    """組み合わせが変わった・セッションが閉じた: この準備はやめる。"""


class _Preempt(Exception):
    """優先の高いもの（今のトラック）に譲る。待ちに戻す。"""


class _Defer(Exception):
    """ほかのプロセスのエンジンが準備している: 少し後に回す。"""


class _Fallback(Exception):
    """解析は済んだが、ガイドとの対応付けに失敗して位置のままの対応になった（#32）: 準備済みとは言わない。"""


class Incomplete(ProjectError):
    """準備済みの印に要るキャッシュが欠けている（印を書かない）。"""


# ---------------------------------------------------------------- 署名と済みの印
def track_sig(s, t):
    """準備の組み合わせの署名（テイクの素材・範囲・セッションのガイドの切り出し・方式・ピッチ検出の方式）。"""
    gd = None
    try:
        g, _why = s.guide_clip_for(t)
    except ProjectError:
        g = None
    if g is not None:
        c = M.as_clip(g)
        gt = s.track(s.guide)
        gd = [_norm(c.source), gt.get("sha256"), int(c.offset_frames or 0), c.length_frames,
              bool(c.pad)]
    d = [PREP_VERSION, _norm(t["path"]), t.get("sha256"), t.get("clip"), t.get("source_id"), gd,
         ALIGN_METHOD]
    est = F0.resolve_estimator(s.estimator_of(t))     # トラックで明示した方式があればそれ
    if est != "rmvpe":
        d.append([est, F0.estimator_version(est)])   # ピッチ検出の方式（RMVPE は前と同じ署名のまま）
    return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _stamp_path(pdir):
    return os.path.join(pdir, "cache", STAMP_FILE)


STAMP_KEEP = 8                       # 済みの印を残す組み合わせの数（ガイドの付け外し・A→B→A で準備をやり直さない）


def _stamp_entries(pdir):
    """`cache/prepared.json` の済みの組み合わせ（新しい順）。"""
    try:
        d = read_json(_stamp_path(pdir))
    except (OSError, ValueError):
        return []
    if not isinstance(d, dict) or d.get("version") != PREP_VERSION:
        return []
    return [e for e in d.get("entries") or [] if isinstance(e, dict) and e.get("sig")]


def has_stamp(pdir, sig):
    """この入力の準備済み印があるか（内容の有効性は `is_ready` で確かめる）。"""
    return any(e["sig"] == sig for e in _stamp_entries(pdir))


def lyrics_sig(entries):
    """テイクの歌詞の鍵（音素の入力。準備済みの印に残し、歌詞が変わったら準備し直す）。"""
    raw = json.dumps(LY.key_of(LY.normalize(entries) or []), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


_lyrics_memo = {}                    # プロジェクト → ((project.json の更新時刻, サイズ, ID), 歌詞の鍵)


def disk_lyrics_sig(pdir):
    """ディスクの project.json のテイクの歌詞の鍵（無ければ None）。メモリに置いた同じ内容の Project があれば
    それを、無ければ project.json が変わったときだけ読む。"""
    p = _store.cached_project(pdir)
    if p is not None:
        return lyrics_sig(p.lyrics_entries("take"))
    path = os.path.join(pdir, "project.json")
    try:
        st = os.stat(path)
    except OSError:
        return None
    key, sig = _norm(pdir), (st.st_mtime_ns, st.st_size, st.st_ino)
    hit = _lyrics_memo.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    try:
        d = read_json(path)
    except (OSError, ValueError):
        return None
    value = lyrics_sig((d.get("lyrics") or {}).get("take"))
    _lyrics_memo[key] = (sig, value)
    return value


def stamp_state(pdir, sig):
    """このトラックのこの組み合わせの印: (READY, 印) / (FAILED, 印)（対応付けに失敗して位置のままの対応。#32）/
    (None, None)（まだ・印のキャッシュが欠けた・歌詞が変わった）。

    組み合わせ（ガイドの指定・位置）ごとに最近の `STAMP_KEEP` 件を覚えている（issue #63 の 3）。準備済みと
    言えるのは、印に書いた必要なファイル（テイクの解析・ガイドの解析・対応付け・発音の頭・音素）が全部、
    同じ大きさ・内容ハッシュで残っていて、テイクの歌詞が印を書いたときのままのとき。"""
    for e in _stamp_entries(pdir):
        if e["sig"] != sig:
            continue
        if e.get("failed"):
            return FAILED, e
        files = e.get("files") or []
        if not files or not all(isinstance(f, list) and len(f) == 3 for f in files):
            return None, None                    # 前の版の印（存在だけを見ていた）: 準備し直す（キャッシュを読むだけ）
        for path, size, digest in files:
            try:
                if os.path.getsize(path) != size or _cache_hash(path) != digest:
                    return None, None
            except OSError:
                return None, None
        if e.get("lyrics") != disk_lyrics_sig(pdir):
            return None, None
        return READY, e
    return None, None


def _cache_hash(path):
    # 同じ版の解析キャッシュは切り替え・ガイド変更のたびに照合するので、内容の計算を使い回す。
    st = os.stat(path)
    sig = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
    with _cache_hash_lock:
        hit = _cache_hash_memo.get(path)
        if hit is not None and hit[0] == sig:
            return hit[1]
    h = hashlib.blake2s(digest_size=16)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    after = os.stat(path)
    if sig == (after.st_mtime_ns, after.st_size, after.st_ino, after.st_ctime_ns):
        with _cache_hash_lock:
            if len(_cache_hash_memo) >= 512:
                _cache_hash_memo.pop(next(iter(_cache_hash_memo)))
            _cache_hash_memo[path] = (sig, digest)
    return digest


_cache_hash_memo = {}
_cache_hash_lock = threading.Lock()


def _forget_hashes(pdir):
    """pdir の下のファイルの内容ハッシュの使い回しを捨てる（キャッシュが壊れていた）。"""
    key = _norm(pdir)
    with _cache_hash_lock:
        for path in [p for p in _cache_hash_memo if _under(key, p)]:
            del _cache_hash_memo[path]


def _drop_stamps(pdir, path):
    """path を含む済みの印を外す。外した署名の集合を返す。"""
    key = _norm(path)
    stamp = _stamp_path(pdir)
    with dir_lock(pdir):
        entries = _stamp_entries(pdir)
        keep = [e for e in entries
                if not any(isinstance(f, list) and f and _norm(f[0]) == key for f in e.get("files") or [])]
        dropped = {e["sig"] for e in entries} - {e["sig"] for e in keep}
        if dropped and os.path.isdir(os.path.dirname(stamp)):
            tmp = _store._tmp_name(stamp)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": PREP_VERSION, "entries": keep}, f, ensure_ascii=False)
            replace_file(tmp, stamp)
    return dropped


def _remove_invalid_caches(pdir, sig):
    """印の書き込み後に壊れたキャッシュだけ外し、解析で作り直せるようにする。"""
    for entry in _stamp_entries(pdir):
        if entry["sig"] != sig:
            continue
        for file in entry.get("files") or []:
            if not isinstance(file, list) or len(file) != 3:
                continue
            path, size, digest = file
            try:
                invalid = os.path.getsize(path) != size or _cache_hash(path) != digest
            except OSError:
                continue
            if invalid:
                try:
                    data = read_json(path)
                    name = os.path.basename(path)
                    if isinstance(data, dict) and (
                            (name.endswith("-analysis.json") and "f0" in data and "notes" in data)
                            or (name == "onsets.json" or name.startswith("onsets-"))
                            and "key" in data and "sec" in data
                            or name == "alignment.json" and "method" in data
                            or "phonemes" in name and "phonemes" in data):
                        continue             # 別エンジンが有効な解析結果を更新した
                except (OSError, ValueError):
                    pass
                try:
                    os.remove(path)
                except OSError:
                    pass
        return


def stamp_ok(pdir, sig):
    """このトラックがこの組み合わせで準備済みか（`stamp_state`）。"""
    return stamp_state(pdir, sig)[0] == READY


def _running_path(pdir):
    return os.path.join(pdir, "cache", RUNNING_FILE)


def foreign_running(pdir):
    """ほかのプロセスがこのトラックを準備中か（`prep.lock` を握られている）。"""
    lk = FileLock(os.path.join(pdir, PREP_LOCK_FILE))
    try:
        if not lk.try_acquire():
            return True
    except OSError:
        return False
    lk.release()
    return False


def _touch_running(pdir):
    path = _running_path(pdir)
    try:
        if not os.path.isdir(pdir):               # 消された作業場所を作り直さない
            return
        if not os.path.isdir(os.path.dirname(path)):
            os.mkdir(os.path.dirname(path))
        tmp = path + ".%d-%d.tmp" % (os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "at": time.time()}, f)
        replace_file(tmp, path)
    except OSError:
        pass


def _heartbeat(pdir):
    """準備の間、`preparing.json` の時刻を HEARTBEAT_SEC ごとに更新する（長い段の途中でも古くならない）。
    止めるときは (stop, thread) の stop を立てて thread を待つ。"""
    stop = threading.Event()

    def loop():
        while True:
            _touch_running(pdir)
            if stop.wait(HEARTBEAT_SEC):
                return
    th = threading.Thread(target=loop, daemon=True, name="gliss-prep-beat")
    th.start()
    return stop, th


def _clear_running(pdir):
    path = _running_path(pdir)
    try:
        with open(path, encoding="utf-8") as f:
            if json.load(f).get("pid") != os.getpid():
                return
        os.remove(path)
    except (OSError, ValueError):
        pass


def _required_files(p):
    """準備済みと言うのに要るキャッシュ（選んだときの analyze_take がキャッシュを読むだけで済むもの）。"""
    need = [os.path.join(p.dir, "cache", "take-analysis.json")]
    if p.guide is not None:
        g = p._guide_cache_path
        if not g:
            raise Incomplete("ガイドの解析の置き場が決まっていない")
        need += [os.path.join(g, "guide-analysis.json"), os.path.join(g, "onsets.json"),
                 os.path.join(p._alignment_cache_dir(), "alignment.json"),
                 os.path.join(p.dir, "cache", "onsets-take.json")]
    for role in ("take", "guide"):
        if role == "guide" and p.guide is None:
            continue
        entries = p.lyrics_entries(role)
        if entries and p.phoneme_error(role) is None:    # 音素が切れなかった歌詞は、切れなかったことを覚えている
            need.append(p._keyed_phoneme_cache(role, entries))
    return need


def write_stamp(p, sig, before_write=None):
    """解析が済んだ Project（表・裏どちらでも）に、組み合わせの署名を残す。書いた印（dict）を返す。

    - 必要なキャッシュ（`_required_files`）が 1 つでも欠けていたら、印を書かずに `Incomplete`
    - ガイドとの対応付けに失敗して位置のままの対応になった（#32）: 「失敗」の印を書く（`failed`）。
      準備済みとは言わないが、同じ組み合わせでは自動でやり直さない（選んだときに表で取り直す）
    before_write: 書く直前（トラックのロックの中）に呼ぶ（裏の準備の取り消しの確かめ。例外で書かない）。"""
    at = time.strftime("%Y-%m-%dT%H:%M:%S")
    fb = p._srcs.get("alignment") if p.guide is not None else None
    if fb and fb[0] == "fallback":
        entry = {"sig": sig, "failed": "ガイドとの対応付けに失敗した（位置のままの対応で表示）: %s" % fb[1],
                 "files": [], "at": at, "lyrics": lyrics_sig(p.lyrics_entries("take"))}
    else:
        files = []
        missing = []
        for f in _required_files(p):
            try:
                files.append([f, os.path.getsize(f), _cache_hash(f)])
            except OSError:
                missing.append(os.path.relpath(f, p.dir))
        if missing:
            raise Incomplete("準備のキャッシュが欠けている: %s" % ", ".join(missing))
        entry = {"sig": sig, "files": files, "at": at, "lyrics": lyrics_sig(p.lyrics_entries("take"))}
    path = _stamp_path(p.dir)
    with dir_lock(p.dir):                        # 表（mark_ready）と裏が同時に足しても、片方を落とさない
        if before_write is not None:
            before_write()
        if not os.path.isdir(os.path.dirname(path)):
            p.sub("cache")                       # 裏の準備は消されたディレクトリを作り直さない（`_need_dir`）
        entries = [entry] + [e for e in _stamp_entries(p.dir) if e["sig"] != sig][:STAMP_KEEP - 1]
        tmp = _store._tmp_name(path)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": PREP_VERSION, "entries": entries}, f, ensure_ascii=False)
        replace_file(tmp, path)
    return entry


# ---------------------------------------------------------------- 準備の 1 件
class _Item:
    def __init__(self, sdir, tid, pdir, sig):
        self.sdir, self.tid, self.pdir, self.sig = sdir, tid, pdir, sig
        self.state = QUEUED
        self.stage = None
        self.progress = 0.0
        self.error = None
        self.cancelled = False
        self.preempt = False
        self.waiting = False            # 表の重い処理・一時停止で、次の段へ進むのを待っている
        self.started_at = None
        self.elapsed_sec = None
        self.defer_until = 0.0
        self.stage_sec = {}             # 段ごとの秒（最後の準備。計測・ログ用。待った時間を含む）
        self.wait_sec = 0.0             # 表の重い処理・一時停止で待った秒
        self._stage_t0 = None
        self.lyrics = None              # 準備済みにしたときのテイクの歌詞の鍵（変わったら準備し直す）
        self.retry = True               # 失敗したとき、合流で 1 回やり直すか（対応付けの失敗 #32 はやり直さない）
        self.delay = 0.0                # テスト用: 段の頭ごとに待つ秒（_test_delay）

    def to_json(self):
        return {"state": self.state, "stage": self.stage,
                "stage_label": STAGE_LABELS.get(self.stage) if self.stage else None,
                "progress": round(float(self.progress), 3) if self.state == PREPARING else
                (1.0 if self.state == READY else None),
                "error": self.error, "paused": bool(self.waiting) if self.state == PREPARING else False,
                "elapsed_sec": self.elapsed_sec}


class Preparer:
    def __init__(self):
        self._cv = threading.Condition()
        self._sdir = None
        self._items = {}                # トラック id → _Item（今の組み合わせのもの）
        self._order = []                # セッションの並び順
        self._guide = None
        self._current = None
        self._recent = []               # 最近選んだトラック（前ほど新しい）
        self._running = None
        self._claimed = {}              # 表が占有しているプロジェクトのディレクトリ → 数
        self._joining = {}              # 表が合流して待っているトラック id → 数
        self._busy = 0                  # 表の重い処理の数（その間は次の段へ進まない）
        self._paused = False
        self._thread = None
        self._after = []                # (ディレクトリ, 関数): ワーカーがそこから離れたら呼ぶ（作業場所を消す）
        self.history = []               # 済んだ準備（計測・テスト用。最近の 50 件）

    # ------------------------------------------------------------ 入れる
    def schedule(self, s, current=Ellipsis):
        """セッションのボーカルのトラックを順番待ちに入れる（署名が変わったものは入れ直す）。
        current: 表の編集対象（省略時は前のまま）。"""
        if not enabled() or s is None:
            return
        with self._cv:
            self._schedule_locked(s, current)
        self._ensure_thread()

    def _schedule_locked(self, s, current=Ellipsis):
        if self._sdir != s.dir:
            for it in self._items.values():
                it.cancelled = True
            self._items, self._recent, self._current = {}, [], None
            self._sdir = s.dir
        if current is not Ellipsis:
            self._current = current
            if current:
                self._recent = [current] + [x for x in self._recent if x != current][:31]
        want, order = {}, []
        for t in s.tracks:
            if t["kind"] != "vocal" or not t.get("project_dir"):
                continue
            try:
                if not os.path.exists(t["path"]):
                    continue
                pdir = s.project_dir_of(t)
                sig = track_sig(s, t)
            except (ProjectError, OSError):
                continue
            tid = t["id"]
            order.append(tid)
            it = self._items.get(tid)
            if it is not None and it.sig == sig and _norm(it.pdir) == _norm(pdir):
                if it.state == READY and not stamp_ok(pdir, sig):
                    it.state = QUEUED               # 歌詞・キャッシュが変わった: 準備し直す
                want[tid] = it
                continue
            if it is not None:
                it.cancelled = True             # 走っていれば次の段の境目で止まる
            new = _Item(s.dir, tid, pdir, sig)
            self._from_stamp(new, *stamp_state(pdir, sig))
            want[tid] = new
            if it is not None or new.state != READY:
                log.get("prep").info("裏の準備に入れる: %s（%s）", tid, new.state)
        for tid, it in self._items.items():
            if tid not in want:
                it.cancelled = True
        self._items, self._order, self._guide = want, order, s.guide
        self._cv.notify_all()

    @staticmethod
    def _from_stamp(it, state, entry):
        """済みの印（`stamp_state`）の状態を item に写す。"""
        if state == READY:
            it.state, it.progress, it.error, it.lyrics = READY, 1.0, None, entry.get("lyrics")
        elif state == FAILED:
            it.state, it.error, it.retry = FAILED, entry.get("failed"), False

    def reset(self):
        """セッションを閉じた: 全部やめる。"""
        with self._cv:
            for it in self._items.values():
                it.cancelled = True
            self._items, self._order, self._recent = {}, [], []
            self._sdir = self._current = self._guide = None
            self._cv.notify_all()

    def release_tree(self, root, timeout=None):
        """root の下のトラックの準備をやめ、ワーカーがそこから離れるまで待つ（作業場所を消す・空にする前）。

        離れたら（初めから中にいなければ）True。timeout 秒（既定 RELEASE_WAIT_SEC）たっても段の途中なら False
        （呼び手はエンジンのロックを握っているので長くは待たない。消すなら `after_leave` に預ける）。
        ワーカーは取り消しを、段の境目と、project.json・済みの印を書く直前（トラックのロックの中）で確かめる。"""
        timeout = RELEASE_WAIT_SEC if timeout is None else timeout
        key = _norm(root)

        def under(p):
            return _under(key, p)
        with self._cv:
            if self._sdir is not None and under(self._sdir):
                for it in self._items.values():
                    it.cancelled = True
                self._items, self._order, self._recent = {}, [], []
                self._sdir = self._current = self._guide = None
            else:
                for tid, it in list(self._items.items()):
                    if under(it.pdir):
                        it.cancelled = True
                        del self._items[tid]
            self._cv.notify_all()
            t0 = time.time()
            while self._running is not None and under(self._running.pdir) and time.time() - t0 < timeout:
                self._cv.wait(min(0.2, max(0.001, timeout - (time.time() - t0))))
            return not (self._running is not None and under(self._running.pdir))

    def after_leave(self, root, fn):
        """ワーカーが root の下から離れたら fn を呼ぶ（今いなければすぐ呼ぶ）。`release_tree` が時間切れのときに、
        作業場所を消すのを後に回す。"""
        key = _norm(root)
        with self._cv:
            later = self._running is not None and _under(key, self._running.pdir)
            if later:
                self._after.append((key, fn))
        if not later:
            fn()

    # ------------------------------------------------------------ 状態
    def status(self, sdir, tid):
        with self._cv:
            it = self._items.get(tid) if sdir and self._sdir == sdir else None
            return it.to_json() if it is not None else None

    def overview(self, sdir=None):
        with self._cv:
            if sdir is not None and self._sdir != sdir:
                return {"enabled": enabled(), "session_dir": sdir, "tracks": [], "running": None,
                        "paused": self._paused}
            run = self._running
            return {"enabled": enabled(), "session_dir": self._sdir, "current": self._current,
                    "paused": self._paused, "running": run.tid if run is not None else None,
                    "tracks": [dict(self._items[t].to_json(), id=t) for t in self._order
                               if t in self._items]}

    def is_ready(self, s, t):
        """このトラックの今の組み合わせが準備済みか（準備を止めていても、済みの印で分かる）。"""
        try:
            sig = track_sig(s, t)
            pdir = s.project_dir_of(t)
        except (ProjectError, OSError):
            return False
        with self._cv:
            it = self._items.get(t["id"]) if self._sdir == s.dir else None
            if it is None or it.sig != sig:
                it = None
            elif it.state != READY:
                return False
            else:
                lyr = it.lyrics
        if it is None:
            return stamp_ok(pdir, sig)
        if lyr == disk_lyrics_sig(pdir) and stamp_ok(pdir, sig):
            return True
        # 準備した後で歌詞が変わった（外部の書き換えも）: 音素を準備し直す（ほかの段はキャッシュを読むだけ）
        with self._cv:
            if it.state == READY and it.lyrics == lyr:
                it.state = QUEUED
                self._cv.notify_all()
        self._ensure_thread()
        return False

    def saved(self, p):
        """表の Project を保存した（`store.PREP_SAVED`。トラックのロックの中）。準備済みのトラックの歌詞が
        変わっていたら（set_lyrics・歌詞の取り込み・音節の変更・取り消し…）、音素を裏で準備し直す。"""
        key = _norm(p.dir)
        again = False
        with self._cv:
            for it in self._items.values():
                if it.state == READY and _norm(it.pdir) == key and \
                        it.lyrics != lyrics_sig(p.lyrics_entries("take")):
                    it.state = QUEUED
                    # 表が今すぐ音素を取り直すこと（set_lyrics の reanalyze）が多いので、少し待ってから
                    it.defer_until = time.time() + LYRICS_DEFER_SEC
                    again = True
            if again:
                self._cv.notify_all()
        if again:
            self._ensure_thread()

    def mark_ready(self, s, t, p):
        """表の analyze_take が済んだ（既定の設定で）: 済みの印を書き、待ちから外す。
        対応付けに失敗して位置のままの対応だった（#32）なら「失敗」の印（準備済みにはしない）。"""
        try:
            sig = track_sig(s, t)
            state, entry = stamp_state(p.dir, sig)
            if state is None:
                entry = write_stamp(p, sig)
        except Exception as e:                   # noqa: BLE001  印が書けなくても解析は済んでいる
            log.get("prep").warning("準備済みの印を書けない: %s", e)
            return
        with self._cv:
            it = self._items.get(t["id"]) if self._sdir == s.dir else None
            if it is not None and it.sig == sig and it.state in (QUEUED, FAILED, READY):
                if entry.get("failed"):
                    it.state, it.error, it.retry = FAILED, entry["failed"], False
                else:
                    it.state, it.progress, it.error, it.lyrics = READY, 1.0, None, entry.get("lyrics")
                self._cv.notify_all()

    def broken(self, pdir, path):
        """準備済みのキャッシュ（path）を読んで失敗した（`store.PREP_BROKEN`。ファイルは呼び手が外した）。
        そのファイルを含む済みの印と、トラックの内容ハッシュの使い回しを取り消し、準備し直す（順番待ちへ戻す。
        表は `join` / `front` で合流して待つ）。"""
        _forget_hashes(pdir)
        dropped = _drop_stamps(pdir, path)
        key = _norm(pdir)
        again = False
        with self._cv:
            for it in self._items.values():
                if _norm(it.pdir) == key and it.state == READY and it.sig in dropped:
                    it.state, it.error, it.retry, it.defer_until = QUEUED, None, True, 0.0
                    again = True
            if again:
                self._cv.notify_all()
        log.get("prep").warning("準備済みのキャッシュが壊れていた。印を外して準備し直す: %s（印 %d 件）",
                                path, len(dropped))
        if again:
            self._ensure_thread()

    # ------------------------------------------------------------ 合流・占有・一時停止
    def join(self, sdir, tid, report=None, cancel=None):
        """トラックの準備を最優先にして、終わるまで待つ。終わった状態（to_json）か、準備に無ければ None。
        report(progress, stage) を待つ間に呼ぶ（例外を上げれば待つのをやめる。準備は続く）。"""
        with self._cv:
            it = self._items.get(tid) if self._sdir == sdir else None
            if it is None:
                return None
            if it.state == FAILED:
                if not it.retry:                 # 対応付けの失敗（#32）: 同じ組み合わせではやり直さない（表で取り直す）
                    return it.to_json()
                it.state, it.error = QUEUED, None   # 表から頼まれた: もう一度やってみる
            self._current = tid
            self._recent = [tid] + [x for x in self._recent if x != tid]
            self._joining[tid] = self._joining.get(tid, 0) + 1
            it.defer_until = 0.0
            self._cv.notify_all()
        self._ensure_thread()
        deadline = None if cancel is not None else time.monotonic() + FRONT_WAIT_SEC
        try:
            while True:
                with self._cv:
                    it = self._items.get(tid) if self._sdir == sdir else None
                    if it is None:
                        return None
                    if it.state in (READY, FAILED):
                        return it.to_json()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise PreparationPending("準備中。%.0f 秒待っても完了しないため、後でやり直す" % FRONT_WAIT_SEC)
                    self._cv.wait(0.1)
                    snap = (it.progress, it.stage)
                if report is not None:
                    report(*snap)
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("合流を取り消した")
        finally:
            with self._cv:
                n = self._joining.get(tid, 1) - 1
                if n > 0:
                    self._joining[tid] = n
                else:
                    self._joining.pop(tid, None)

    def _unjoin(self, tid):
        n = self._joining.get(tid, 1) - 1
        if n > 0:
            self._joining[tid] = n
        else:
            self._joining.pop(tid, None)

    def _must_wait(self, key):
        """front: 表がこのディレクトリの計算を始める前に、裏の準備を待つべきか。待つなら合流する item（か、
        取り消されて段の終わりへ向かっている、走っている item）。"""
        run = self._running
        for it in self._items.values():
            if _norm(it.pdir) != key:
                continue
            if it.state == PREPARING and it is run:
                return None if it.stage == "view" else it     # 描画データの段: 解析はもう済んでいる
            if it.state == QUEUED and key not in self._claimed:
                return it                        # 順番待ち: 最優先にして待つ（計算は裏の 1 回）
        if run is not None and _norm(run.pdir) == key:
            return run                           # 取り消された準備が段の終わりに向かっている（すぐ離れる）
        return None

    @contextlib.contextmanager
    def front(self, pdir, wait=True):
        """表がこのディレクトリを自分で解析する（`store.PREP_FRONT`。ensure_analyzed・音素）。

        - 裏の準備が順番待ち（queued）・準備中なら、最優先にして（合流）終わるまで待つ。計算は裏の 1 回で、
          表はその後キャッシュを読む。待つ間に準備が取り消された（ガイドの指定が変わった）ら、同じトラックの
          新しい準備を待つ。セッションを閉じた（reset）ら待つのをやめて、表で計算する
        - その後（準備に無い・失敗した・待たないとき）は、このディレクトリを**占有**する（`exclusive` と同じ）。
          表が計算している間、裏は同じトラックを始めない（原子的に順番待ちから外す）
        - wait=False（どのみち計算し直す force）・今のスレッドがトラックのロックを握っているとき（ワーカーの
          書き込みと行き詰まる）は待たずに占有する。走っていれば次の段の境目で譲らせる"""
        key = _norm(pdir)
        if threading.current_thread() is self._thread:
            yield
            return
        wait = wait and not dir_lock(pdir).held()
        deadline = time.monotonic() + FRONT_WAIT_SEC
        joined = None
        with self._cv:
            try:
                while wait:
                    it = self._must_wait(key)
                    if it is None:
                        break
                    if time.monotonic() >= deadline:
                        raise PreparationPending("準備中。%.0f 秒待っても完了しないため、後でやり直す" % FRONT_WAIT_SEC)
                    if joined != it.tid:
                        if joined is not None:
                            self._unjoin(joined)
                        joined = it.tid
                        self._joining[joined] = self._joining.get(joined, 0) + 1
                        it.defer_until = 0.0         # 後回しにしていても、待たれているので今見直す
                        self._cv.notify_all()
                        self._ensure_thread()
                    self._cv.wait(0.2)
            finally:
                if joined is not None:
                    self._unjoin(joined)
            self._claimed[key] = self._claimed.get(key, 0) + 1
            if self._running is not None and _norm(self._running.pdir) == key:
                self._running.preempt = True     # 待たなかった: 次の段の境目で譲らせる
        try:
            yield
        finally:
            with self._cv:
                n = self._claimed.get(key, 1) - 1
                if n > 0:
                    self._claimed[key] = n
                else:
                    self._claimed.pop(key, None)
                self._cv.notify_all()

    @contextlib.contextmanager
    def exclusive(self, pdir):
        """表がこのディレクトリを自分で解析する間（force・別の推定器）、裏の準備は触らない（走っていれば譲らせる）。"""
        key = _norm(pdir)
        with self._cv:
            self._claimed[key] = self._claimed.get(key, 0) + 1
            if self._running is not None and _norm(self._running.pdir) == key:
                self._running.preempt = True
            while self._running is not None and _norm(self._running.pdir) == key:
                self._cv.wait(0.2)
        try:
            yield
        finally:
            with self._cv:
                n = self._claimed.get(key, 1) - 1
                if n > 0:
                    self._claimed[key] = n
                else:
                    self._claimed.pop(key, None)
                self._cv.notify_all()

    @contextlib.contextmanager
    def foreground(self):
        """表の重い処理（再生前の音作り・試聴・書き出し）の間、裏の準備は次の段へ進まない。"""
        with self._cv:
            self._busy += 1
        try:
            yield
        finally:
            with self._cv:
                self._busy -= 1
                self._cv.notify_all()

    def set_paused(self, paused):
        with self._cv:
            self._paused = bool(paused)
            self._cv.notify_all()

    # ------------------------------------------------------------ ワーカー
    def _ensure_thread(self):
        with self._cv:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True, name="gliss-prep")
            self._thread.start()

    def _hold(self, it=None):
        """次へ進まずに待つか（表の重い処理・一時停止。合流されているものは待たない）。"""
        if it is not None and it.tid in self._joining:
            return False
        return self._paused or (self._busy > 0 and _yield_enabled())

    def _pick(self):
        now = time.time()
        cands = [it for it in self._items.values()
                 if it.state == QUEUED and _norm(it.pdir) not in self._claimed
                 and it.defer_until <= now and (not self._hold(it))]
        if not cands:
            return None

        def rank(it):
            if it.tid in self._joining:
                return (0, 0)
            if it.tid == self._current:
                return (1, 0)
            if it.tid == self._guide:
                return (2, 0)
            if it.tid in self._recent:
                return (3, self._recent.index(it.tid))
            return (4, self._order.index(it.tid) if it.tid in self._order else 999)
        return min(cands, key=rank)

    def _check(self, it):
        """段の境目: 取り消し・譲る・待つ。"""
        with self._cv:
            while True:
                if it.cancelled or self._sdir != it.sdir or self._items.get(it.tid) is not it:
                    raise _Cancel()
                if it.preempt or _norm(it.pdir) in self._claimed:
                    raise _Preempt()
                if it.tid not in self._joining:
                    for tid in list(self._joining) + [self._current]:
                        o = self._items.get(tid)
                        if o is not None and o is not it and o.state == QUEUED \
                                and _norm(o.pdir) not in self._claimed:
                            raise _Preempt()
                if self._hold(it):
                    it.waiting = True
                    t0 = time.perf_counter()
                    self._cv.wait(0.2)
                    it.wait_sec += time.perf_counter() - t0
                    continue
                it.waiting = False
                return

    def _enter(self, it, name, check=True):
        with self._cv:
            now = time.perf_counter()
            if it.stage and it._stage_t0 is not None:
                it.stage_sec[it.stage] = round(it.stage_sec.get(it.stage, 0.0) + now - it._stage_t0, 3)
            it._stage_t0 = now
            it.stage = name
            it.progress = max(it.progress, _STAGE_PROGRESS.get(name, it.progress))
            self._cv.notify_all()
        if check and it.delay > 0:
            end = time.perf_counter() + it.delay
            while time.perf_counter() < end and not it.cancelled:
                time.sleep(0.05)
        if check:
            self._check(it)

    def _advance(self, it, value):
        with self._cv:
            it.progress = max(it.progress, min(0.99, float(value)))
            self._cv.notify_all()
        self._check(it)

    def _alive(self, it, pdir):
        """書き込みの直前（トラックのロックの中）: 取り消し・作業場所の破棄・セッションの入れ替えをもう一度確かめる。
        段の境目の確かめの後に長く止まっていても、消した作業場所へ古い内容を書き戻さない。"""
        with self._cv:
            if it.cancelled or self._sdir != it.sdir or self._items.get(it.tid) is not it:
                raise _Cancel()
        if not os.path.isdir(pdir):
            raise _Cancel()

    def _loop(self):
        while True:
            try:
                self._step()
            except Exception:                    # noqa: BLE001  ワーカーは止めない
                log.get("prep").exception("裏の準備のワーカーで例外")
                time.sleep(1.0)

    def _step(self):
        """1 件を選んで準備する（無ければ待つ）。"""
        with self._cv:
            it = self._pick()
            while it is None:
                self._cv.wait(0.5)
                it = self._pick()
            it.state, it.stage, it.progress = PREPARING, None, 0.0
            it.preempt, it.waiting, it.error, it.retry = False, False, None, True
            it.stage_sec, it._stage_t0, it.wait_sec = {}, None, 0.0
            it.started_at = time.time()
            self._running = it
            self._cv.notify_all()
        t0 = time.perf_counter()
        outcome, err = READY, None
        try:
            self._run(it)
        except _Cancel:
            outcome = "cancel"
        except _Preempt:
            outcome = QUEUED
        except _Defer:
            outcome = "defer"
        except _Fallback as e:
            outcome, err = FAILED, str(e)
            it.retry = False
        except Exception as e:               # noqa: BLE001
            if it.cancelled:                     # 取り消した後の失敗（作業場所が消えた等）
                outcome = "cancel"
            else:
                outcome, err = FAILED, str(e) or type(e).__name__
                log.get("prep").exception("裏の準備に失敗: %s（%s）", it.tid, it.pdir)
        dt = time.perf_counter() - t0
        with self._cv:
            self._running = None
            it.waiting = False
            if it.stage and it._stage_t0 is not None:
                it.stage_sec[it.stage] = round(it.stage_sec.get(it.stage, 0.0)
                                               + time.perf_counter() - it._stage_t0, 3)
            if outcome == READY:
                it.state, it.progress, it.stage = READY, 1.0, None
                it.elapsed_sec = round(dt, 3)
            elif outcome == FAILED:
                it.state, it.error, it.stage = FAILED, err, None
            elif outcome in (QUEUED, "defer") or self._items.get(it.tid) is it:
                it.state, it.stage = QUEUED, None
                if outcome == "defer":           # 合流して待たれていれば早めに見直す
                    it.defer_until = time.time() + (0.5 if it.tid in self._joining else DEFER_SEC)
            self.history = (self.history + [{"track": it.tid, "outcome": outcome,
                                             "sec": round(dt, 3), "sig": it.sig,
                                             "stages": dict(it.stage_sec),
                                             "wait_sec": round(it.wait_sec, 3)}])[-50:]
            later = [fn for k, fn in self._after if _under(k, it.pdir)]
            self._after = [(k, fn) for k, fn in self._after if not _under(k, it.pdir)]
            self._cv.notify_all()
        log.get("prep").info("裏の準備: %s → %s（%.2f s。%s）", it.tid, outcome, dt,
                             " ".join("%s %.2f" % kv for kv in it.stage_sec.items()))
        for fn in later:                         # 離れるのを待っていた後始末（作業場所を消す）
            try:
                fn()
            except Exception:                    # noqa: BLE001
                log.get("prep").exception("準備が離れた後の後始末に失敗")

    def _run(self, it):
        s = Session.load(it.sdir)                # 表の Session オブジェクトには触らない
        try:
            t = s.track(it.tid)
        except ProjectError:
            raise _Cancel()
        if track_sig(s, t) != it.sig:
            # session.json が表の知っているものより新しい（外部の書き換え）: そちらに合わせて入れ直す
            with self._cv:
                if self._sdir == s.dir:
                    self._schedule_locked(s)
            raise _Cancel()
        pdir = s.project_dir_of(t)
        if self._done_elsewhere(it, pdir):
            return                               # ほかのプロセス（Claude Code のエンジン）が済ませた
        if not os.path.isdir(pdir):
            if not os.path.isdir(it.sdir):
                raise _Cancel()                  # 作業場所が消された
            os.makedirs(pdir, exist_ok=True)
        # ほかのプロセスのエンジンと同じトラックを同時に準備しない（OS のロック。取れなければ後回し）
        lk = FileLock(os.path.join(pdir, PREP_LOCK_FILE))
        try:
            if not lk.try_acquire():
                raise _Defer()
        except OSError:
            raise _Cancel()
        stop, beat = _heartbeat(pdir)
        try:
            if self._done_elsewhere(it, pdir):   # ロックを取る直前に、ほかのプロセスが済ませた
                return
            _remove_invalid_caches(pdir, it.sig)
            self._prepare(it, s, t)
        finally:
            stop.set()
            beat.join(2.0)
            _clear_running(pdir)
            lk.release()

    @staticmethod
    def _done_elsewhere(it, pdir):
        state, entry = stamp_state(pdir, it.sig)
        if state == READY:
            it.lyrics = entry.get("lyrics")
            return True
        if state == FAILED:
            raise _Fallback(entry.get("failed"))
        return False

    def _prepare(self, it, s, t):
        it.delay = _test_delay(t.get("path"))
        self._enter(it, "open")
        q, _why = s.prep_project_for(t)
        self._check(it)

        def alive():
            self._alive(it, q.dir)

        def commit(_action):
            self._enter(it, "commit")           # ここを過ぎたら段の境目では取り消さない（書く直前にもう一度確かめる）
            with dir_lock(q.dir):
                alive()
                Project(q.dir).load().merge_background(q, before_write=alive)

        for attempt in range(4):
            try:
                q.analyze(progress=lambda v: self._advance(it, v), stage=lambda n: self._enter(it, n),
                          commit=commit)
                break
            except _store.CacheBroken:
                if attempt == 3:             # 壊れたファイルは 1 回ごとに外れる。外した分を計算し直す
                    raise
        entry = write_stamp(q, it.sig, before_write=alive)
        it.lyrics = entry.get("lyrics")
        self._build_view(it, q)
        if entry.get("failed"):
            raise _Fallback(entry["failed"])

    def _build_view(self, it, q):
        """選んだときの描画データ（export_view_data。`cache/view/<鍵>.json`）を作っておく（issue #63 の 3）。

        鍵は表の Project と同じ入力から作るので、表でこのトラックを選べばそのまま使われる（入力が違えば
        使われないだけ）。解析はもう済んでいるので、取り消し・譲るの確認はしない（0.2〜0.6 秒）。
        失敗しても準備は済んだまま（表で選んだときに作る）。"""
        if not VIEW_ENABLED:
            return
        self._enter(it, "view", check=False)
        try:
            from .view.export_data import export_view_data
            export_view_data(q)
        except Exception as e:                   # noqa: BLE001
            log.get("prep").warning("描画データを作れない（選んだときに作る）: %s（%s）", it.tid, e)


PREPARER = Preparer()
_store.PREP_FRONT = PREPARER.front
_store.PREP_RELEASE = PREPARER.release_tree
_store.PREP_AFTER_LEAVE = PREPARER.after_leave
_store.PREP_SAVED = PREPARER.saved
_store.PREP_BROKEN = PREPARER.broken

schedule = PREPARER.schedule
reset = PREPARER.reset
status = PREPARER.status
overview = PREPARER.overview
is_ready = PREPARER.is_ready
mark_ready = PREPARER.mark_ready
join = PREPARER.join
front = PREPARER.front
exclusive = PREPARER.exclusive
foreground = PREPARER.foreground
set_paused = PREPARER.set_paused
