# -*- coding: utf-8 -*-
"""プロジェクトの保存・読み込みと編集リストの適用。

ディレクトリ構成:
    <project>/
        project.json     元音声の参照（パス＋SHA-256＋ソース ID＋ソース内オフセット）・編集リスト・取り消し履歴
        sources/         サンプル列で渡されたソースを WAV にしたもの（`media.Samples`）
        engine.log       エンジンのログ（stderr は捨てられるので自前で書く）
        cache/           解析結果（F0・音符・DTW）
        views/           render_view の PNG（毎回ユニークなファイル名）
        renders/         render_preview の WAV
"""
import hashlib
import json
import os
import sys
import threading
from collections import OrderedDict
from threading import Lock

import numpy as np

from .. import log
from ..analysis.align import DEFAULT_METHOD as ALIGN_METHOD
from ..analysis.align import ALIGN_VERSION, Alignment, boundary_deviations, deviations
from ..analysis.f0 import ENERGY_FLOOR_DB, RMVPE_THRESHOLD, F0Result, estimate_f0
from ..analysis import f0 as f0mod
from ..analysis.notes import Note, segment_notes
from ..audio import file_sig, sha256_file
from .. import media as M
from ..phoneme import lyrics as LY
from .model import Changeset, Edit, Target, now_iso

def _default_projects_root():
    """旧形式のプロジェクト（`.gliss` でない）の既定の置き場。配布版（単体 exe）はインストール先の外
    （`%LOCALAPPDATA%\\Gliss\\projects`）、開発版は `<リポジトリ>/projects`。"""
    from .. import config
    if config.frozen():
        return os.path.join(config.user_data_dir(), "projects")
    return os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "..", "projects"))


_DEFAULT_PROJECTS_ROOT = _default_projects_root()
PROJECTS_ROOT = os.environ.get("VOCAL_ENGINE_PROJECTS") or _DEFAULT_PROJECTS_ROOT


def projects_root():
    """旧形式のプロジェクトの置き場。`VOCAL_ENGINE_PROJECTS` を渡していなくて作業場所（`VOCAL_ENGINE_WORK_DIR`）を
    渡していれば、その下の `projects`（作業場所を分けて動かす AI・テストが `<リポジトリ>/projects` を汚さない）。"""
    if PROJECTS_ROOT != _DEFAULT_PROJECTS_ROOT or os.environ.get("VOCAL_ENGINE_PROJECTS"):
        return PROJECTS_ROOT
    work = os.environ.get("VOCAL_ENGINE_WORK_DIR")
    return os.path.join(os.path.abspath(work), "projects") if work else PROJECTS_ROOT
SCHEMA_VERSION = 2              # 2: take / guide にソース ID とソース内オフセット（media.py）
ARCHIVE_FORMAT = "vocal-editor-archive"
ARCHIVE_VERSION = 1


AUTO_LYRICS_CACHE_VERSION = 1   # cache/auto-lyrics.json（歌詞の自動推定の結果）の形・作り方を変えたら上げる
MIN_TIMING_PAIRS = 3       # これ未満しか発音の頭の組が作れない素材は、ガイドを DTW の写像で描く

# 元音声はファイルの署名が同じ間使い回し、158 秒の WAV を毎回読み直す待ち時間をなくす。
# 1 本 約 64 MB（float64 のモノラル）なので、トラック 3 本 + ずらしたガイドでも追い出さない大きさにする（issue #63）
_AUDIO_CACHE_LIMIT = 512 * 1024 * 1024
_audio_cache_lock = Lock()
_shared_audio_cache = OrderedDict()
_shared_audio_bytes = 0


def _shared_audio(info, key):
    global _shared_audio_bytes
    st = os.stat(info["path"])
    shared_key = (os.path.normcase(os.path.abspath(key[0])), key[1], key[2],
                  info.get("sha256"), st.st_size, st.st_mtime_ns, st.st_ino)
    with _audio_cache_lock:
        hit = _shared_audio_cache.get(shared_key)
        if hit is not None:
            _shared_audio_cache.move_to_end(shared_key)
            return hit
    value = M.read_clip(info, mono=True)
    size = value[0].nbytes
    if size > _AUDIO_CACHE_LIMIT:
        return value
    with _audio_cache_lock:
        hit = _shared_audio_cache.get(shared_key)
        if hit is not None:
            _shared_audio_cache.move_to_end(shared_key)
            return hit
        _shared_audio_cache[shared_key] = value
        _shared_audio_bytes += size
        while _shared_audio_bytes > _AUDIO_CACHE_LIMIT:
            _, old = _shared_audio_cache.popitem(last=False)
            _shared_audio_bytes -= old[0].nbytes
    return value


class ProjectError(RuntimeError):
    pass


class CacheBroken(ProjectError):
    """解析のキャッシュが読めない（JSON として読めない・中身が足りない）。壊れたファイルは外し、
    準備済みの印と内容ハッシュの使い回しも取り消してある（`Project._cache_broken`）。呼び手は準備し直す。"""

    def __init__(self, path, err):
        super().__init__("解析のキャッシュが壊れていた（準備し直す）: %s: %s" % (os.path.basename(path), err))
        self.path = path


# 解析のキャッシュが読めないときの例外（JSON の壊れ・キーや型の不足）。OSError（無い・開けない）は含めない
_BROKEN_ERRORS = (ValueError, KeyError, TypeError, AttributeError, IndexError)


class ProjectConflict(ProjectError):
    """読んだ後で別のエンジンが保存した。呼び手は最新状態を確認し直す。"""


class FileLock:
    """プロセスをまたぐロック（OS のファイルのロック。Windows は `msvcrt.locking`、ほかは `fcntl.flock`）。

    ロック用のファイルの先頭 1 バイトを排他で押さえる。ロックはファイルを開いたハンドルに付くので、
    プロセスが落ちれば OS が外す（落ちたプロセスのロックが残ることはない。ファイルそのものは残るが無害）。
    同じプロセスの中でも、別のインスタンス同士はぶつかる（入れ子にするなら `dir_lock` を使う）。"""

    def __init__(self, path):
        self.path = path
        self._fd = None

    def try_acquire(self):
        """取れたら True。取れなければすぐ False。ロック用のファイルを置くディレクトリが無ければ OSError。"""
        if self._fd is not None:
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o666)
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def acquire(self, timeout):
        """timeout 秒まで待って取る。取れなければ False。"""
        import time as _t
        t0 = _t.monotonic()
        wait = 0.002
        while not self.try_acquire():
            if _t.monotonic() - t0 >= timeout:
                return False
            _t.sleep(wait)
            wait = min(wait * 2, 0.05)
        return True

    def release(self):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)


# トラックのプロジェクト（ディレクトリ）ごとのロック。裏の準備（`prep.py`。issue #63）と表の操作
# （select_track・編集の保存）と、**別のプロセスのエンジン**（画面と Claude Code）が、同じ project.json を
# 同時に書かないようにする。握るのは project.json を読み書きする短い間だけ（重い計算の間は握らない）。
# プロセスの中はスレッドのロック（入れ子にできる）、プロセスの間は `<プロジェクト>/project.lock` の OS のロック
LOCK_FILE = "project.lock"
DIR_LOCK_TIMEOUT = 10.0         # 別のプロセスがこの秒より長く握っていたら、待たずに失敗する（ProjectError）
_dir_locks = {}
_dir_locks_lock = Lock()


class _DirLock:
    def __init__(self, path):
        self.dir = path
        self._rlock = threading.RLock()
        self._depth = 0
        self._owner = None
        self._file = None

    def __enter__(self):
        self._rlock.acquire()
        try:
            if self._file is None:
                self._file = self._lock_file()
        except BaseException:
            self._rlock.release()
            raise
        self._depth += 1
        self._owner = threading.get_ident()
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            f, self._file, self._owner = self._file, None, None
            if f is not None:
                f.release()
        self._rlock.release()
        return False

    def held(self):
        """今のスレッドが握っているか。"""
        return self._depth > 0 and self._owner == threading.get_ident()

    def _lock_file(self):
        if not os.path.isdir(self.dir):
            return None         # まだ無い（作る前）・消された: プロセスの中だけ（入れ子の中で作られたら、そこで取る）
        f = FileLock(os.path.join(self.dir, LOCK_FILE))
        try:
            ok = f.acquire(DIR_LOCK_TIMEOUT)
        except OSError:          # 取る間にディレクトリが消えた
            return None
        if not ok:
            raise ProjectError("別のエンジンが %s を %.0f 秒以上書き込み中（少し待ってからやり直す）"
                               % (self.dir, DIR_LOCK_TIMEOUT))
        return f


def dir_lock(path):
    key = os.path.normcase(os.path.abspath(path))
    with _dir_locks_lock:
        lk = _dir_locks.get(key)
        if lk is None:
            lk = _dir_locks[key] = _DirLock(os.path.abspath(path))
        return lk


def warm_media(*clips):
    """音声ファイルの SHA-256 と情報（`sf.info`）を先に取っておく（プロセスの中で覚える。`audio.sha256_file`・
    `media.file_info`）。トラックごとのロックの中の `Project.open` で、大きな WAV を読まないため（issue #63）。"""
    for c in clips:
        if c is None:
            continue
        c = M.as_clip(c)
        if c.is_samples:
            continue
        path = os.path.abspath(c.source)
        if os.path.exists(path):
            sha256_file(path)
            M.file_info(path)


def front(pdir, wait=True):
    """表がこのトラックを自分で解析する間（`ensure_analyzed`・音素）。裏の準備が順番待ち・準備中なら、最優先に
    して終わりを待ち（合流。計算は裏の 1 回）、その後はそのトラックを占有して裏が同じ計算を始めないようにする
    （`prep.Preparer.front`）。wait=False は待たずに占有だけする（どのみち計算し直す force のとき）。
    同じスレッドで入れ子になったら何もしない。enter の値は、待った・占有したか。"""
    import contextlib
    key = os.path.normcase(os.path.abspath(pdir))
    held = _front_tls.__dict__.setdefault("dirs", set())
    if PREP_FRONT is None or key in held:
        return contextlib.nullcontext(False)

    @contextlib.contextmanager
    def ctx():
        held.add(key)
        try:
            with PREP_FRONT(pdir, wait=wait):
                yield True
        finally:
            held.discard(key)
    return ctx()


_front_tls = threading.local()


# 開いた Project をメモリに置いておく（issue #63）。トラックを選ぶたびに project.json を読み直して編集リストを
# 当て直していた（編集 409 件で 0.03〜0.1 秒）。使うのは project.json がディスク上で、その Project が前に読んだ・
# 書いたときのまま（`_disk_sig`）のときだけ。外部（Claude Code・裏の準備）が書き換えていれば今までどおり読み直す
_OPEN_KEEP = 8
_open_cache = OrderedDict()
_open_lock = Lock()


def remember_open(p):
    key = os.path.normcase(os.path.abspath(p.dir))
    with _open_lock:
        _open_cache[key] = p
        _open_cache.move_to_end(key)
        while len(_open_cache) > _OPEN_KEEP:
            _open_cache.popitem(last=False)


def cached_project(pdir):
    """メモリに置いてある、ディスクの project.json と同じ内容の Project（無ければ None）。"""
    key = os.path.normcase(os.path.abspath(pdir))
    with _open_lock:
        p = _open_cache.get(key)
    if p is None or p._disk_sig is None or p._json_sig() != p._disk_sig:
        return None
    return p


def forget_open():
    """メモリに置いた Project を捨てる（曲を閉じた）。"""
    with _open_lock:
        _open_cache.clear()


def recorded_estimator(analysis):
    """解析の要約（project.json の analysis）から、テイクを前に解析した F0 の方式。まだ解析していなければ None。
    方式を記録する前の要約（方式が RMVPE だけだったころ）は "rmvpe"。"""
    ta = (analysis or {}).get("take")
    if not ta:
        return None
    return ta.get("estimator") or "rmvpe"


_recorded_cache = {}                # project.json のパス -> (署名, 方式)
_recorded_lock = Lock()


def recorded_estimator_in(pdir):
    """ディレクトリの project.json に記録された F0 の方式（`recorded_estimator`）。裏の準備の署名
    （`prep.track_sig`）が、開いていないトラックについても曲ごとの方式を知るため。project.json が変わったときだけ読む。"""
    path = os.path.join(pdir, "project.json")
    sig = _src_sig(path)
    if sig is None:
        return None
    key = sig[0]
    with _recorded_lock:
        hit = _recorded_cache.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    try:
        est = recorded_estimator(read_json(path).get("analysis"))
    except (OSError, ValueError, AttributeError):
        return None
    with _recorded_lock:
        if len(_recorded_cache) > 256:
            _recorded_cache.clear()
        _recorded_cache[key] = (sig, est)
    return est


def _src_sig(path):
    """解析ファイルの署名（パス・更新時刻・サイズ・ID）。描画の鍵と読み直し判定に使う。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (os.path.normcase(os.path.abspath(path)), st.st_mtime_ns, st.st_size, st.st_ino)


_code_sig_value = None


def _code_sig():
    """エンジンのコード（vocal_engine/ の .py）の版。描画データの作り方を変えたら、前に作ったものを使わない。"""
    global _code_sig_value
    if _code_sig_value is None:
        if getattr(sys, "frozen", False):
            with open(os.path.join(sys._MEIPASS, "gliss-build-version.txt"), encoding="ascii") as f:
                _code_sig_value = f.read().strip()
            return _code_sig_value
        from .. import __version__
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        h = hashlib.sha1(__version__.encode("utf-8"))
        for d, _dirs, files in sorted(os.walk(root)):
            for name in sorted(files):
                if name.endswith(".py"):
                    st = os.stat(os.path.join(d, name))
                    h.update(("%s|%s|%d|%d;" % (os.path.relpath(d, root), name, st.st_size,
                                                st.st_mtime_ns)).encode("utf-8"))
        _code_sig_value = h.hexdigest()[:16]
    return _code_sig_value


def _stable(analysis, drop=("analyzed_at",)):
    """解析の要約から、解析した時刻を除いたもの（中身が同じか比べる）。"""
    return {k: ({kk: vv for kk, vv in v.items() if kk not in drop} if isinstance(v, dict) else v)
            for k, v in analysis.items()}


def _edit_state(doc):
    """解析要約と保存時刻を除いた、利用者の編集状態。解析だけの外部保存を見分ける。"""
    fields = ("schema_version", "take", "guide", "lyrics", "seq", "edits", "changesets")
    return json.dumps([doc.get(k) for k in fields], sort_keys=True, ensure_ascii=False)


# 表がトラックを自分で解析する間の context manager（`front` から。`prep.py` が入れる）。裏の準備が
# 順番待ち・準備中なら合流して待ち、その後はトラックを占有する。同じ計算を表と裏で 2 回しないため
PREP_FRONT = None
# 作業場所を消す・空にする前に、裏の準備をそこから離す関数（`prep.py` が入れる。`document.remove_work`）。
# 離れたら True、時間切れなら False
PREP_RELEASE = None
# PREP_RELEASE が時間切れのとき、ワーカーが離れてから呼ぶ関数を預ける（`prep.py` が入れる）
PREP_AFTER_LEAVE = None
# 表の Project を保存した（`prep.py` が入れる）。準備済みのトラックの歌詞が変わっていたら、音素を準備し直す
PREP_SAVED = None
# 解析のキャッシュが読めなかった（`prep.py` が入れる。引数はプロジェクトのディレクトリとファイル）。準備済みの印と
# 内容ハッシュの使い回しを取り消し、そのトラックを準備し直す（issue #63。同サイズ・同時刻の上書きはハッシュの
# 使い回しでは見分けられないので、実際に読んで失敗したところで拾う）
PREP_BROKEN = None


def replace_file(tmp, dst, tries=40, wait=0.025):
    """`os.replace` の Windows 向けの再試行。画面（Electron の main）が project.json / session.json を
    読んでいる瞬間に置き換えると「アクセスが拒否されました」（WinError 5 / 32）になる。
    履歴を持つようになって session.json を編集のたびに書くので、ぶつかることが増えた（issue #16）。"""
    import time as _t
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            _t.sleep(wait)


def read_json(path, tries=40, wait=0.025):
    """JSON のファイルを読む。裏の準備が同じファイルを置き換えている瞬間に開くと Windows では
    「アクセスが拒否されました」になるので、少し待って読み直す（`replace_file` の読む側。issue #63）。"""
    import time as _t
    for i in range(tries):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except PermissionError:
            if i == tries - 1:
                raise
            _t.sleep(wait)


def _note_from_json(d):
    keep = {k: d[k] for k in Note.__dataclass_fields__ if k in d}
    return Note(**keep)


class Project:
    def __init__(self, dir_path):
        self.dir = os.path.abspath(dir_path)
        self.take = None            # audio_info + sha256
        self.guide = None
        self.edits = []             # 有効な編集（replay 済み）
        self.changesets = []
        self._seq = {"edit": 0, "changeset": 0}
        self.analysis = {}          # {"take": {...}, "guide": {...}, "alignment": {...}}
        self.lyrics = {}            # {"take": [{start_sec,end_sec,text}, ...]}（区間ごと）
        self.align_method = ALIGN_METHOD    # DTW の特徴量（段階2 から MFCC が既定）
        self._phonemes = {"take": None, "guide": None}
        # 音素アラインの失敗: role -> (入力の鍵, 例外)。同じ歌詞・音声では操作のたびにやり直さない（issue #58）
        self._phoneme_failed = {}
        self._take_f0 = None
        self._guide_f0 = None
        self._take_notes = None
        self._guide_notes = None
        self._notes_cache = None    # (解析のノート列, split/merge の id 列, 当てた結果)
        self._alignment = None
        self._onsets = {}           # role -> (鍵, 発音の頭の秒)
        self._audio_cache = {}
        self._audio_sigs = {}
        self._note_index = None
        self._media_changed = False
        self._onset_sigs = {}
        self.guide_take_cache_path = None  # セッション中のガイドトラック自身の解析結果
        self._guide_cache_path = None
        # 裏の準備用のインスタンス（`prep.py`。issue #63）: project.json と、今の組み合わせを指す写し
        # （cache/guide-analysis.json など）は書かず、鍵付きの保存だけに書く。project.json へは
        # analyze の commit で、読み直した最新の内容に合わせて書く
        self.background = False
        self.estimator_pref = None       # このトラックで明示した F0 の方式（None なら選んでいる方式。session.py のトラックの estimator）
        self._auto_proposed = []    # 最後の歌詞の自動推定で足した区間
        self._disk_sig = None      # project.json の (mtime_ns, size, file ID)
        self._base_edit_state = None
        self._base_analysis = {}
        # メモリの解析結果を読んだ・書いたファイルの署名（take / guide / alignment / ph_take / ph_guide）。
        # 同じファイルなら読み直さない・描画データの鍵（`view_key`）に入れる（issue #63）
        self._srcs = {}

    # ------------------------------------------------------------ 場所
    @property
    def json_path(self):
        return os.path.join(self.dir, "project.json")

    @property
    def log_path(self):
        return os.path.join(self.dir, "engine.log")

    def f0_estimator(self, estimator=None):
        """この曲を解析する F0 の方式（`f0.resolve_estimator`）。estimator を省くと、このトラックで明示した方式
        （`estimator_pref`）→ 選んだ方式 → この曲を前に解析した方式 → 既定（RMVPE。重みが無ければ Gliss）。
        RMVPE の重みを後から取った・既定を替えたときも、解析・編集済みの曲の音符の区切りは変えない。"""
        if estimator is None:
            estimator = self.estimator_pref
        return f0mod.resolve_estimator(estimator, recorded=recorded_estimator(self.analysis))

    def sub(self, name):
        p = os.path.join(self.dir, name)
        if not os.path.isdir(p):                 # makedirs(exist_ok) は有っても Windows で数 ms かかる
            self._need_dir()
            os.makedirs(p, exist_ok=True)
        return p

    def _need_dir(self):
        """裏の準備は、消されたプロジェクトのディレクトリを作り直さない（作業場所を消した後に古い中身を書き戻さない）。"""
        if self.background and not os.path.isdir(self.dir):
            raise ProjectError("プロジェクトのディレクトリが無い（消された）: %s" % self.dir)

    # ------------------------------------------------------------ 生成・保存
    @classmethod
    def open(cls, take_path, guide_path=None, project_dir=None, reuse=True,
             lyrics=None, guide_lyrics=None, set_log=True, memo=True):
        """テイク（とガイド）を開く。

        take_path / guide_path は **WAV のパス**（段階 2 までと同じ。曲頭 0:00・ファイル全体）か、
        `media.Samples`（サンプル列）か、`media.Clip`（ソースの一部 = ソース内オフセット＋長さ＋ソース ID）。
        プロジェクトの時間は**クリップの頭が 0**（`docs/daw-stage0.md` §1）。

        memo: 前に開いた Project がメモリにあり、project.json がそのときのままなら、読み直さずにそれを使い、
        開いたものをメモリに置く（issue #63）。裏の準備（別のスレッド）は False（表の Project に触らない）。
        """
        p = cls._open(take_path, guide_path, project_dir, reuse, lyrics, guide_lyrics, set_log, memo)
        if memo:
            remember_open(p)
        return p

    def _forget_guide_state(self):
        """メモリのガイド側の解析（F0・対応付け・発音の頭・音素）を捨てる（ガイドを差し替えた・外した）。"""
        self._guide_f0 = None
        self._guide_notes = None
        self._alignment = None
        self._onsets.pop("guide", None)
        self._phonemes["guide"] = None
        self._guide_cache_path = None
        for k in ("guide", "alignment", "ph_guide"):
            self._srcs.pop(k, None)
        self._audio_cache = {}
        self._audio_sigs.pop("guide", None)

    @classmethod
    def _open(cls, take_path, guide_path, project_dir, reuse, lyrics, guide_lyrics, set_log, memo):
        take = M.as_clip(take_path)
        guide = M.as_clip(guide_path) if guide_path else None
        for label, c in (("テイク", take), ("ガイド", guide)):
            if c is not None and not c.is_samples and not os.path.exists(c.source):
                raise ProjectError("%sの音声が見つからない: %s" % (label, os.path.abspath(c.source)))
        take_sha = None if take.is_samples else sha256_file(os.path.abspath(take.source))
        try:
            if project_dir is None:
                project_dir = _default_project_dir(take, take_sha)
            p = cls(project_dir)
            os.makedirs(p.dir, exist_ok=True)
            if set_log:                          # 裏の準備は表のログの行き先を変えない
                log.set_log_file(p.log_path)
            take_info = M.describe(take, M.materialize(take, p.dir), sha256=take_sha, role="take")
            guide_info = None
            if guide is not None:
                guide_info = M.describe(guide, M.materialize(guide, p.dir), role="guide")
        except M.MediaError as e:
            raise ProjectError(str(e))
        if os.path.exists(p.json_path):
            old = cached_project(project_dir) if memo else None
            if old is None:
                old = cls(project_dir)
                try:
                    old.load()
                except ProjectError:
                    raise                            # 新しい版のエンジンで作られた: 上書きしない
                except Exception as e:               # noqa: BLE001  壊れていたら退避して作り直す
                    import time as _t
                    bak = p.json_path + ".broken-%s" % _t.strftime("%Y%m%d-%H%M%S")
                    replace_file(p.json_path, bak)   # 裏の準備が読んでいる瞬間は動かせない（待って取り直す）
                    log.get().warning("project.json を読めないので %s に退避して作り直す: %s", bak, e)
                    old = cls(project_dir)
            same_take = M.same_clip(old.take, take_info)
            same_guide = (guide is None) or M.same_clip(old.guide, guide_info)
            if reuse and same_take and not same_guide:
                # テイクが同じでガイドだけ違う（ガイドを後から開いた・差し替えた）: 編集リストと歌詞は
                # テイクのものなので残し、現在のガイドを示す写しだけ入れ替える。
                # cache/guide/<key>/ は後で同じ組み合わせに戻すため残す。
                # （段階 2 までは作り直していて、画面で「ガイドを開く」と編集が全部消えていた）
                log.get().warning("ガイドを差し替える（編集 %d 件は残す）: %s", len(old.edits), p.dir)
                for name in ("guide-analysis.json", "alignment.json", "guide-phonemes.json",
                             "onsets-guide.json"):
                    _remove(os.path.join(p.dir, "cache", name))
                for k in ("guide", "alignment", "phonemes_guide"):
                    old.analysis.pop(k, None)
                g_lyr = _shift_guide_lyrics(old.guide, guide_info, old.lyrics.get("guide"))
                old.guide = guide_info
                old.lyrics.pop("guide", None)
                if g_lyr:
                    old.lyrics["guide"] = g_lyr
                old._forget_guide_state()            # メモリに置いてあった Project なら、前のガイドの解析を持っている
                old.save()
                same_guide = True
            if reuse and same_take and same_guide:
                p = old
                log.get().info("既存プロジェクトを開いた: %s（編集 %d 件）", p.dir, len(p.edits))
                moved = False
                for role, info in (("take", take_info), ("guide", guide_info)):
                    cur = getattr(p, role)
                    if info is None or cur is None:
                        continue
                    # 同じ中身: 場所（ファイルが移った・サンプル列を書き直した）と ID・名前だけ今のものに
                    for k in ("path", "sha256", "source_id", "source_name", "content_sha256"):
                        if cur.get(k) != info.get(k):
                            cur[k] = info.get(k)
                            moved = True
                if moved:
                    p._audio_cache = {}
                    p.save()
                for role, val in (("take", lyrics), ("guide", guide_lyrics)):
                    if val is None:
                        continue
                    if LY.normalize(val) != p.lyrics.get(role, []):
                        p.set_lyrics_entries(val, role)
                return p
            if not same_take:
                # 素材（中身かクリップの範囲）が違う。解析のキャッシュは前の素材のものなので捨てる
                log.get().warning("素材が変わったのでプロジェクトを作り直す: %s", p.dir)
                p._clear_cache()
            else:
                log.get().warning("プロジェクトを作り直す（reuse=%s・ガイド %s）: %s", reuse,
                                  "同じ" if same_guide else "が違う", p.dir)
                if not same_guide:
                    for name in ("guide-analysis.json", "alignment.json", "guide-phonemes.json",
                                 "onsets-guide.json"):
                        _remove(os.path.join(p.dir, "cache", name))
        p.take = take_info
        p.guide = guide_info
        for role, val in (("take", lyrics), ("guide", guide_lyrics)):
            ent = LY.normalize(val)
            if ent:
                p.lyrics[role] = ent
        p.save()
        log.get().info("プロジェクトを作成: %s（ソース %s / オフセット %d / %d サンプル）", p.dir,
                       take_info["source_id"], take_info["offset_frames"], take_info["frames"])
        return p

    def clear_guide(self):
        """ガイドを外す（セッションでガイドの指定を外した・編集対象がガイドのトラック自身）。

        編集リストとテイクの歌詞は残す。現在のガイドの解析の写しとガイドの歌詞は捨てる
        （ガイドを差し替えるときと同じ）。外していなければ何もしない。"""
        if self.guide is None:
            return False
        log.get().warning("ガイドを外す（編集 %d 件は残す）: %s", len(self.edits), self.dir)
        for name in ("guide-analysis.json", "alignment.json", "guide-phonemes.json",
                     "onsets-guide.json"):
            _remove(os.path.join(self.dir, "cache", name))
        for k in ("guide", "alignment", "phonemes_guide"):
            self.analysis.pop(k, None)
        self.guide = None
        self.lyrics.pop("guide", None)
        self._forget_guide_state()
        self.save()
        return True

    def _clear_cache(self):
        d = os.path.join(self.dir, "cache")
        if os.path.isdir(d):
            for name in os.listdir(d):
                _remove(os.path.join(d, name))

    def to_json(self, copy=True):
        """project.json の中身。copy=False は changeset の中身をコピーせずに入れる（すぐ書き出すだけのとき）。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "dir": self.dir,
            "updated_at": now_iso(),
            "take": self.take,
            "guide": self.guide,
            "lyrics": self.lyrics,
            "seq": self._seq,
            "edits": [e.to_json() for e in self.edits],
            "changesets": [c.to_json() if copy else c.to_json_shallow() for c in self.changesets],
            "analysis": self.analysis,
        }

    def save(self):
        """project.json を書く（トラックごとのロックの中。別のプロセスのエンジンともぶつからない）。

        前に読んだ・書いた後で編集状態が変わっていたら、上書きせずに読み直して競合を返す。
        解析要約だけが変わった場合は、編集を再実行せずその要約を取り込む。"""
        if not os.path.isdir(self.dir):
            self._need_dir()
            os.makedirs(self.dir, exist_ok=True)
        with dir_lock(self.dir):
            if self._changed_on_disk():
                latest = Project(self.dir).load()
                if self._base_edit_state is not None and latest._base_edit_state == self._base_edit_state:
                    # 裏の準備などが解析要約だけを書いた。編集の計算はやり直さず、その要約を現在の編集に足す。
                    merged = dict(latest.analysis)
                    for key in set(self.analysis) | set(self._base_analysis):
                        if self.analysis.get(key) != self._base_analysis.get(key):
                            if key in self.analysis:
                                merged[key] = self.analysis[key]
                            else:
                                merged.pop(key, None)
                    self.analysis = merged
                    self._disk_sig = latest._disk_sig
                else:
                    self.load()
                    raise ProjectConflict("別のエンジンが project.json を更新した。最新の内容を確認してやり直す: %s"
                                          % self.dir)
            tmp = _tmp_name(self.json_path)      # 別のプロセス・スレッドと書きかけのファイルを共有しない
            try:
                doc = self.to_json(copy=False)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(json.dumps(doc, ensure_ascii=False, indent=2))
                replace_file(tmp, self.json_path)
            except BaseException:
                _remove(tmp)
                raise
            self._disk_sig = self._json_sig()
            self._base_edit_state = _edit_state(doc)
            self._base_analysis = json.loads(json.dumps(self.analysis))
            if not self.background and PREP_SAVED is not None:
                PREP_SAVED(self)
        return self.json_path

    def _changed_on_disk(self):
        """前に読んだ・書いた後で、project.json がディスク上で書き換わったか（読んだことが無ければ False）。"""
        if self._disk_sig is None:
            return False
        sig = self._json_sig()
        return sig is not None and sig != self._disk_sig

    def _json_sig(self):
        try:
            st = os.stat(self.json_path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def reload_if_changed(self):
        """project.json が**ディスク上で変わっていたら読み直す**。

        画面（Electron）と外部のエージェント（Claude Code）が同じプロジェクトを
        同時に触るため。編集を適用する前に必ず呼ぶ。保存時にも版を照合し、
        途中で変われば ProjectConflict を返す。解析キャッシュは別に照合する。
        """
        sig = self._json_sig()
        if sig is None or sig == self._disk_sig:
            return False
        log.get().info("project.json が外部で変わっていたので読み直す: %s", self.json_path)
        self.load()
        return True

    def load(self):
        for _ in range(3):
            sig = self._json_sig()
            d = read_json(self.json_path)
            if sig == self._json_sig():
                break
        else:
            raise ProjectConflict("project.json を読み込む間に更新された。もう一度開く: %s" % self.dir)
        old_g = self.guide
        if int(d.get("schema_version") or 1) > SCHEMA_VERSION:
            raise ProjectError("project.json の schema %s はこのエンジン（%d まで）では読めない（新しい版で作られた）"
                               % (d.get("schema_version"), SCHEMA_VERSION))
        self.take = M.normalize_media(d.get("take"))     # schema 1（段階 2 まで）はソースのキーが無い
        self.guide = M.normalize_media(d.get("guide"))
        self.lyrics = {k: LY.normalize(v) for k, v in (d.get("lyrics") or {}).items()}
        self.lyrics = {k: v for k, v in self.lyrics.items() if v}
        self._phonemes = {"take": None, "guide": None}
        self._srcs.pop("ph_take", None)
        self._srcs.pop("ph_guide", None)
        self._seq = d.get("seq", {"edit": 0, "changeset": 0})
        self.changesets = [Changeset.from_json(c) for c in d.get("changesets", [])]
        self.analysis = d.get("analysis", {})
        self._disk_sig = sig
        self._base_edit_state = _edit_state(d)
        self._base_analysis = json.loads(json.dumps(self.analysis))
        if old_g is not None and not M.same_clip(old_g, self.guide):
            # 外部（別のプロセス）がガイドを差し替えた・ずらした: 前のガイドの解析と対応付けを使わない
            self._forget_guide_state()
        self._replay()
        return self

    # ------------------------------------------------------------ アーカイブ（UI と無関係の状態）
    def to_archive(self):
        """**編集の状態だけ**を UI・マシンと無関係な dict にする（ARA のアーカイブに入れる単位）。

        入るもの: 素材の参照（ソース ID・SHA-256・ソース内オフセット・長さ。パスは手がかり）、
        歌詞、編集リスト（changeset の列 = 取り消し履歴ごと）、採番、DTW の方式。
        入らないもの: プロジェクトのディレクトリ、解析のキャッシュ（F0・ノート・音素。素材から作り直せる）、
        画面の状態（表示範囲・選択・ツール。画面は userData の `state.json` に別に持つ）、ログ、時刻の更新日。
        JSON にそのまま書ける（数値・文字列・真偽・None・配列・dict だけ）。
        """
        def ref(m):
            if not m:
                return None
            keep = ("source_id", "source_kind", "source_name", "sha256", "sr", "channels",
                    "subtype", "source_frames", "offset_frames", "frames", "path", "pad")
            d = {k: m.get(k) for k in keep}
            d["clip_audio_sha256"] = M.clip_audio_hash(m)     # 渡し方（ファイル／サンプル列）によらない照合
            return d
        return {
            "format": ARCHIVE_FORMAT,
            "version": ARCHIVE_VERSION,
            "engine_schema": SCHEMA_VERSION,
            "take": ref(self.take),
            "guide": ref(self.guide),
            "lyrics": {k: [dict(e) for e in v] for k, v in self.lyrics.items()},
            "auto_lyrics_attempted": "auto_lyrics" in self.analysis,
            "align_method": self.align_method,
            "seq": dict(self._seq),
            "changesets": [c.to_json() for c in self.changesets],
        }

    @classmethod
    def from_archive(cls, archive, take=None, guide=None, project_dir=None, overwrite=False):
        """`to_archive()` の dict から編集の状態を戻す。

        take / guide: 素材（パス・`Samples`・`Clip`）。省略するとアーカイブの `path` を使う。
        ARA ならホストから受け取ったオーディオソースを渡す。範囲（オフセット・長さ）を渡さなければ
        アーカイブのものを使う。**素材の中身（SHA-256）がアーカイブと違えば ProjectError**
        （別の素材に編集を当てない）。解析（F0・ノート・音素）は素材から作り直す
        （`project_dir` に同じ素材のキャッシュがあればそれを使う）。
        `project_dir` に**別の編集履歴を持つ**プロジェクトがあれば ProjectError（`overwrite=True` で上書き）。
        省略時の置き場は画面と同じ既定のディレクトリなので、画面で編集中のものを黙って消さないため。
        """
        if archive.get("format") != ARCHIVE_FORMAT:
            raise ProjectError("アーカイブの形式が違う: %r" % archive.get("format"))
        if int(archive.get("version", 0)) > ARCHIVE_VERSION:
            raise ProjectError("アーカイブの版 %s はこのエンジン（%d まで）では読めない"
                               % (archive.get("version"), ARCHIVE_VERSION))

        def clip_of(given, ref):
            if ref is None:
                return M.as_clip(given) if given is not None else None
            c = M.as_clip(given if given is not None else ref.get("path"))
            if c is None:
                raise ProjectError("素材が渡されていない（アーカイブにパスも無い）")
            if ref.get("pad"):
                c.pad = True                         # セッションのガイド（ソースの外を無音で詰めた）
            if c.offset_frames is None and c.offset_sec is None:
                c.offset_frames = int(ref.get("offset_frames") or 0)
            if c.length_frames is None and c.length_sec is None:
                c.length_frames = int(ref["frames"])
            if c.source_id is None:
                c.source_id = ref.get("source_id")
            return c

        tref, gref = archive.get("take"), archive.get("guide")
        if not tref:
            raise ProjectError("アーカイブにテイクの参照が無い")
        tclip, gclip = clip_of(take, tref), clip_of(guide, gref)
        # 素材の照合は**ディレクトリに触る前に**（違う素材で既存のプロジェクトを作り直さない）
        for role, c, ref in (("take", tclip, tref), ("guide", gclip, gref)):
            if c is None or ref is None:
                continue
            if not c.is_samples and not os.path.exists(c.source):
                raise ProjectError("%s の音声が見つからない: %s" % (role, c.source))
            try:
                if c.is_samples:
                    sr, total = int(c.source.sr), c.source.array().shape[0]
                else:
                    import soundfile as _sf
                    _i = _sf.info(os.path.abspath(c.source))
                    sr, total = int(_i.samplerate), int(_i.frames)
                off, n = M.resolve_range(c, sr, total)
            except M.MediaError as e:
                raise ProjectError(str(e))
            if off != int(ref.get("offset_frames") or 0) or n != int(ref["frames"]):
                raise ProjectError("%s のクリップの範囲がアーカイブと違う（オフセット %s / 長さ %s ≠ %d / %d）"
                                   % (role, ref.get("offset_frames"), ref["frames"], off, n))
            if not c.is_samples and ref.get("sha256") and \
                    sha256_file(os.path.abspath(c.source)) == ref["sha256"]:
                continue
            if not ref.get("clip_audio_sha256") or M.clip_hash_of(c) != ref["clip_audio_sha256"]:
                raise ProjectError("%s の素材の中身がアーカイブと違う（別の音に編集を当てない）" % role)
        if project_dir is None:
            tsha = None if tclip.is_samples else sha256_file(os.path.abspath(tclip.source))
            project_dir = _default_project_dir(tclip, tsha)
        existing = os.path.join(os.path.abspath(project_dir), "project.json")
        if not overwrite and os.path.exists(existing):
            try:
                with open(existing, encoding="utf-8") as f:
                    cur = json.load(f).get("changesets") or []
            except Exception:                        # noqa: BLE001
                cur = None
            if cur is None or (cur and cur != archive.get("changesets", [])):
                raise ProjectError("%s には別の編集履歴がある（上書きするなら overwrite=True）"
                                   % project_dir)
        p = cls.open(tclip, gclip, project_dir=project_dir, reuse=True)
        p.lyrics = {k: LY.normalize(v) for k, v in (archive.get("lyrics") or {}).items()}
        p.lyrics = {k: v for k, v in p.lyrics.items() if v}
        if archive.get("auto_lyrics_attempted"):
            p.analysis["auto_lyrics"] = {"attempted": True, "entries": 0}
        p.align_method = archive.get("align_method") or p.align_method
        p._seq = dict(archive.get("seq") or {"edit": 0, "changeset": 0})
        p.changesets = [Changeset.from_json(c) for c in archive.get("changesets", [])]
        p._phonemes = {"take": None, "guide": None}
        p._notes_cache = None
        p._replay()
        p.save()
        return p

    # ------------------------------------------------------------ 音声
    def _check_audio_versions(self):
        changed = False
        for role in ("take", "guide"):
            info = self.take if role == "take" else self.guide
            if info is None:
                self._audio_sigs.pop(role, None)
                continue
            sig = file_sig(info["path"])
            old = self._audio_sigs.get(role)
            if old is not None and old != sig:
                changed = True
                if sig is not None:
                    meta = M.file_info(info["path"])
                    full = int(info.get("offset_frames", 0)) == 0 and info["frames"] == info.get("source_frames")
                    info.update(sr=int(meta.samplerate), channels=int(meta.channels), subtype=meta.subtype,
                                source_frames=int(meta.frames),
                                source_duration_sec=round(int(meta.frames) / int(meta.samplerate), 6),
                                sha256=sha256_file(info["path"]))
                    if full:
                        info["frames"] = int(meta.frames)
                    info["duration_sec"] = round(info["frames"] / info["sr"], 6)
            self._audio_sigs[role] = sig
        if changed:
            self._audio_cache.clear()
            self._take_f0 = self._guide_f0 = self._take_notes = self._guide_notes = None
            self._alignment = self._notes_cache = None
            self._phonemes = {"take": None, "guide": None}
            self._phoneme_failed.clear()
            self._onsets.clear()
            self._onset_sigs.clear()
            self._srcs.clear()
            self._guide_cache_path = None
            self.analysis.clear()
            self._media_changed = True
        return changed

    def audio(self, role="take"):
        self._check_audio_versions()
        info = self.take if role == "take" else self.guide
        if info is None:
            raise ProjectError("%s の音声が無い" % role)
        # クリップの範囲も鍵に入れる（同じファイルの別の範囲を取り違えない）。x[0] = クリップの頭
        key = (info["path"], int(info.get("offset_frames", 0) or 0), int(info["frames"]),
               self._audio_sigs.get(role))
        if key not in self._audio_cache:
            if not os.path.exists(info["path"]):
                raise ProjectError("音声ファイルが見つからない: %s" % info["path"])
            # 裏の準備は共有の音声キャッシュに入れない（表で開いているトラックの音声を追い出さない）
            x, sr = M.read_clip(info, mono=True) if self.background else _shared_audio(info, key)
            self._audio_cache[key] = (x, sr)
        return self._audio_cache[key]

    def source_sec(self, t, role="take"):
        """プロジェクト（クリップ内）の秒 → ソース上の秒。"""
        info = self.take if role == "take" else self.guide
        return float(info.get("offset_sec", 0.0) or 0.0) + float(t)

    @property
    def duration_sec(self):
        return float(self.take["duration_sec"]) if self.take else 0.0

    # ------------------------------------------------------------ 解析
    def _cache_path(self, name):
        return os.path.join(self.sub("cache"), name)

    @staticmethod
    def _publish_cache(src, dst):
        """既存の読み手向けの現在値を、途中の JSON を見せずに置き換える。

        写しは元の更新時刻に合わせる。サイズと更新時刻が元と同じなら、もう同じものなので書かない（issue #63）。"""
        import shutil
        st = os.stat(src)
        try:
            dt = os.stat(dst)
            if dt.st_size == st.st_size and dt.st_mtime_ns == st.st_mtime_ns:
                return
        except OSError:
            pass
        tmp = _tmp_name(dst)
        import time as _t
        for i in range(40):                     # 裏の準備が元を置き換えている瞬間は開けない（read_json と同じ）
            try:
                shutil.copyfile(src, tmp)
                break
            except PermissionError:
                if i == 39:
                    raise
                _t.sleep(0.025)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        replace_file(tmp, dst)

    GUIDE_CACHE_KEEP = 8

    @staticmethod
    def _clip_cache_identity(info):
        return [info.get("content_sha256") or info.get("sha256"), info.get("source_id"),
                int(info["sr"]), int(info.get("offset_frames") or 0), int(info["frames"]),
                bool(info.get("pad"))]

    def _guide_cache_dir(self, kind, key):
        """別トラックの Project で準備しても同じ結果に到達する鍵付きの置き場。"""
        digest = hashlib.sha256(json.dumps(key, ensure_ascii=False, sort_keys=True).encode(
            "utf-8")).hexdigest()[:24]
        return os.path.join(self.sub(os.path.join("cache", "guide", kind)), digest)

    def _guide_analysis_cache_dir(self, estimator, sweep):
        """F0・音符・発音の頭はガイドの切り出しと解析設定だけに依存する。"""
        key = [1, self._clip_cache_identity(self.guide), estimator, bool(sweep),
               RMVPE_THRESHOLD, ENERGY_FLOOR_DB]
        if f0mod.estimator_version(estimator) is not None:
            key.append(f0mod.estimator_version(estimator))   # 方式の中身を変えたら作り直す（RMVPE は前と同じ鍵）
        return self._guide_cache_dir("analysis", key)

    def _alignment_cache_dir(self):
        """歌詞や編集で取り直し区間が変わっても、従来どおり DTW を使い回す。"""
        key = [1, self._clip_cache_identity(self.take), self._clip_cache_identity(self.guide),
               self.align_method, self._take_f0.estimator,
               self._take_f0.meta.get("threshold"), bool(self._take_f0.meta.get("sweep"))]
        if self._take_f0.meta.get("version") is not None:
            key.append(self._take_f0.meta["version"])
        if ALIGN_VERSION != 1:
            key.append(["align", ALIGN_VERSION])        # 対応付けの中身を変えたら作り直す（帯の制約）
        return self._guide_cache_dir("alignment", key)

    def _prune_guide_cache(self, kind):
        root = os.path.join(self.dir, "cache", "guide", kind)
        try:
            entries = sorted((os.path.join(root, name) for name in os.listdir(root)
                              if os.path.isdir(os.path.join(root, name))),
                             key=os.path.getmtime, reverse=True)
        except OSError:
            return
        for path in entries[self.GUIDE_CACHE_KEEP:]:
            import shutil
            shutil.rmtree(path, ignore_errors=True)

    def _guide_f0_from_track(self, estimator, sweep):
        """完全に同じ全長音声のテイク解析があれば、その F0 をガイドに使う。"""
        path = self.guide_take_cache_path
        g = self.guide
        if not path or not g or g.get("pad") or int(g.get("offset_frames") or 0) != 0 or \
                int(g["frames"]) != int(g["source_frames"]):
            return None
        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(path)), "project.json"),
                      encoding="utf-8") as f:
                take = M.normalize_media(json.load(f).get("take"))
            if not M.same_clip(take, g):
                return None
            with open(path, encoding="utf-8") as f:
                result = F0Result.from_json(json.load(f)["f0"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        same_estimator = f0mod.same_estimator(result.estimator, result.meta.get("version"), estimator)
        vuv_rule = "%s f0>0 AND rms > %.1f dBFS" % (result.estimator, ENERGY_FLOOR_DB)
        if not same_estimator or bool(result.meta.get("sweep")) != bool(sweep) or \
                result.meta.get("threshold") != RMVPE_THRESHOLD or \
                result.meta.get("vuv_rule") != vuv_rule or result.sr != int(g["sr"]) or \
                result.hop_s != 0.01:
            return None
        expected = int(np.floor(int(g["frames"]) / result.sr / result.hop_s)) + 1
        return result if result.n_frames == expected else None

    def guide_timing(self):
        """ガイドのリズムの基準（ガイドの時刻 + 全体のずれ）と発音の頭の組（`analysis/guide_timing.py`）。

        画面に描くガイドの位置・「ガイドに合わせる」のタイミング・ずれの一覧が同じものを使う。"""
        from ..analysis import guide_timing as GT
        return GT.of_project(self)

    def guide_to_take(self):
        """画面・ずれの一覧でガイドを置く位置（ガイドの秒 → テイクの秒）と、その基準。

        ふだんはタイムライン上のガイドの時刻（全体のずれが 150 ms を超える置き場所の違う素材は
        「ガイドの時刻 + 全体のずれ」。issue #61）。発音の頭の組が作れない素材（158 秒の曲のせりふのテイク
        など、テンポの違う別の演奏）や、ガイドの音声が読めないときは、以前と同じ DTW の写像に
        戻す（基準が決まらないのに一定のずれで描くと、ガイドと比べられなくなる）。"""
        al = self.alignment
        try:
            from ..analysis import guide_timing as GT
            gt = GT.of_project(self, align_lyrics=False)
        except Exception as e:                    # noqa: BLE001  画面は DTW の写像で出す
            log.get().warning("ガイドのリズムの基準を作れない（DTW の写像で描く）: %s", e)
            gt = None
        if gt is None or len(gt.pairs) < MIN_TIMING_PAIRS:
            return (lambda t: al.to_take(t)), {"basis": "dtw", "offset_ms": None,
                                               "same_timeline": False}
        return gt.to_take, {"basis": "guide_time", "offset_ms": round(gt.offset_sec * 1000.0, 1),
                            "measured_offset_ms": round(gt.measured_offset_sec * 1000.0, 1),
                            "reference": gt.basis, "same_timeline": gt.same_timeline}

    def onsets(self, role="take"):
        """発音の頭（秒の列、`analysis/onsets.py`）。cache/onsets-<role>.json に残す。

        鍵は音声のパス・クリップの範囲・検出の版。ガイドを差し替えたり版を上げたら取り直す。"""
        from ..analysis import onsets as ON
        self._check_audio_versions()
        info = self.take if role == "take" else self.guide
        if info is None:
            raise ProjectError("%s の音声が無い" % role)
        if role == "guide":
            sg = self.score_guide_notes()
            if sg is not None:                     # 譜面ガイド: 発音の頭 = 譜面のノートの頭
                return np.array([round(n.start_sec, 4) for n in sg], dtype="float64")
        key = [info["path"], int(info.get("offset_frames", 0) or 0), int(info["frames"]),
               info.get("content_sha256") or info.get("sha256"), ON.VERSION]
        alias = self._cache_path("onsets-%s.json" % role)
        path = (os.path.join(self._guide_cache_path, "onsets.json")
                if role == "guide" and self._guide_cache_path else alias)
        mem = self._onsets.get(role)
        t = mem[1] if mem is not None and mem[0] == key and \
            self._onset_sigs.get(role) == _src_sig(path) else None
        if t is not None:
            return t
        if t is None and os.path.exists(path):
            try:
                d = read_json(path)
                if d.get("key") == key:
                    t = np.asarray(d["sec"], dtype="float64")
            except OSError:
                t = None
            except _BROKEN_ERRORS as e:
                self._cache_broken(path, e)      # 外して印も取り消す（下で取り直す）
                t = None
        save = t is None or not os.path.exists(path)
        if t is None:
            x, sr = self.audio(role)
            t = np.round(ON.detect(x, sr), 4)      # ファイルに書く値と同じにする（読み直しで計画が変わらない）
        if save:
            tmp = _tmp_name(path)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"key": key, "sec": [round(float(v), 4) for v in t]}, f)
            replace_file(tmp, path)
        if path != alias and not self.background:
            self._publish_cache(path, alias)
        self._onsets[role] = (key, t)
        self._onset_sigs[role] = _src_sig(path)
        return t

    def analyze(self, force=False, estimator=None, sweep=False, with_guide=True,
                cancel=None, progress=None, commit=None, auto_lyrics=True, stage=None):
        """F0 → 音符 → （ガイドがあれば）DTW。結果は cache/ に保存する。

        estimator: F0 の方式。省くと `f0_estimator()`（画面の「ピッチ検出の方式」で選んだ方式 → この曲を前に
        解析した方式 → 既定）。保存した解析が別の方式のものなら、テイクの F0 から解析し直す（ガイドの解析は
        方式ごとの鍵付きの保存）。

        stage: 段の名前（"take_f0" / "lyrics" / "guide_f0" / "alignment" / "onsets" / "phonemes"）を
        段に入る前に受け取る関数（裏の準備の進み具合と、段の境目での取り消し。`prep.py`）。
        commit: project.json への書き込みを包む関数（`commit(self.save)`）。"""
        def advance(value):
            if progress is not None:
                progress(value)  # ジョブの中断要求もここで確認する
            elif cancel is not None and cancel.is_set():
                raise InterruptedError("解析を取り消した")

        def enter(name):
            if stage is not None:
                stage(name)

        # キャッシュを読むだけで、解析の要約（時刻を除く）も歌詞も変わらなければ project.json を書かない
        # （issue #63。トラックを選ぶたびに書いていた。編集 409 件で 0.07〜0.1 秒）
        before_analysis = json.loads(json.dumps(self.analysis))
        before = self._analysis_state()

        def persist():
            if self._analysis_state() == before and self._json_sig() == self._disk_sig:
                self.analysis = before_analysis          # 解析した時刻も前のまま
                return
            if commit is not None:
                commit(self._save_analysis)
            else:
                self._save_analysis()

        estimator = self.f0_estimator(estimator)
        publish = not self.background   # 今の組み合わせを指す写しを書くか（裏の準備では書かない）
        advance(0.0)
        t0 = now_iso()
        take_cache = self._cache_path("take-analysis.json")
        reuse = not force and os.path.exists(take_cache)
        if reuse:
            if self._take_f0 is None or self._srcs.get("take") != _src_sig(take_cache):
                self._load_take_analysis(take_cache)     # 同じファイルをもう読んでいれば読み直さない
            # 方式を替えた（画面の「ピッチ検出の方式」・MCP の estimator）: 解析し直す
            reuse = f0mod.same_estimator(self._take_f0.estimator, self._take_f0.meta.get("version"),
                                         estimator)
        if not reuse:
            enter("take_f0")
            x, sr = self.audio("take")
            f0r = estimate_f0(x=x, sr=sr, estimator=estimator, sweep=sweep)
            notes = segment_notes(f0r, source="take")
            self._take_f0, self._take_notes = f0r, notes
            tmp = _tmp_name(take_cache)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"f0": f0r.to_json(), "notes": [n.to_json() for n in notes]},
                          f, ensure_ascii=False)
            replace_file(tmp, take_cache)
            self._srcs["take"] = _src_sig(take_cache)
            log.get().info("テイクを解析: %d フレーム / %d ノート（%.2f s）",
                           f0r.n_frames, len(notes), f0r.elapsed_sec)

        self.analysis["take"] = {
            "analyzed_at": t0, "estimator": self._take_f0.estimator,
            "hop_s": self._take_f0.hop_s, "n_frames": int(self._take_f0.n_frames),
            "n_notes": len(self._take_notes),
            "voiced_ratio": round(float(np.mean(self._take_f0.voiced)), 4),
            "cache": take_cache,
        }
        if self._take_f0.meta.get("version") is not None:
            self.analysis["take"]["estimator_version"] = self._take_f0.meta["version"]
        advance(0.45)

        # 歌詞が未設定の発声区間だけを推定する。初回解析のベース状態とし、
        # その後の手動修正や削除は編集履歴へ入れる。
        self._auto_proposed = []
        if auto_lyrics and "auto_lyrics" not in self.analysis:
            enter("lyrics")
            try:
                self._infer_missing_lyrics()
            except Exception as e:  # 推定失敗はピッチ解析を妨げない
                log.get("phoneme").warning("歌詞の自動推定をスキップ: %s", e)

        if with_guide and self.guide is not None:
            guide_dir = self._guide_analysis_cache_dir(estimator, sweep)
            os.makedirs(guide_dir, exist_ok=True)
            os.utime(guide_dir, None)
            self._guide_cache_path = guide_dir
            g_cache = self._cache_path("guide-analysis.json")
            keyed_guide = os.path.join(guide_dir, "guide-analysis.json")
            if not force and os.path.exists(keyed_guide):
                if self._guide_f0 is None or self._srcs.get("guide") != _src_sig(keyed_guide):
                    self._load_guide_analysis(keyed_guide)
                if publish:
                    self._publish_cache(keyed_guide, g_cache)
            else:
                enter("guide_f0")
                gf0 = self._guide_f0_from_track(estimator, sweep)
                if gf0 is None:
                    gx, gsr = self.audio("guide")
                    gf0 = estimate_f0(x=gx, sr=gsr, estimator=estimator, sweep=sweep)
                gnotes = segment_notes(gf0, source="guide", id_prefix="g")
                self._guide_f0, self._guide_notes = gf0, gnotes
                tmp = _tmp_name(keyed_guide)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"f0": gf0.to_json(), "notes": [n.to_json() for n in gnotes]},
                              f, ensure_ascii=False)
                replace_file(tmp, keyed_guide)
                self._srcs["guide"] = _src_sig(keyed_guide)
                if publish:
                    self._publish_cache(keyed_guide, g_cache)
            self.analysis["guide"] = {
                "analyzed_at": t0, "n_frames": int(self._guide_f0.n_frames),
                "n_notes": len(self._guide_notes), "cache": g_cache}
            advance(0.65)

            a_cache = self._cache_path("alignment.json")
            alignment_dir = self._alignment_cache_dir()
            os.makedirs(alignment_dir, exist_ok=True)
            os.utime(alignment_dir, None)
            keyed_alignment = os.path.join(alignment_dir, "alignment.json")
            cached = None
            if not force and os.path.exists(keyed_alignment):
                sig = _src_sig(keyed_alignment)
                if self._alignment is not None and self._srcs.get("alignment") == sig:
                    cached = self._alignment             # 同じファイルをもう読んでいる
                else:
                    cached = self._read_alignment(keyed_alignment)
                    self._srcs["alignment"] = sig
                if cached.method != self.align_method:
                    cached = None          # 方式を変えたら取り直す（段階2 で MFCC が既定）
            if cached is not None:
                self._alignment = cached
                if publish:
                    self._publish_cache(keyed_alignment, a_cache)
            else:
                enter("alignment")
                from .align_helper import compute_alignment, fallback_alignment
                try:
                    self._alignment = compute_alignment(self, method=self.align_method)
                except Exception as e:           # noqa: BLE001
                    # 対応付けが落ちても解析全体は落とさない（issue #32。落とすとガイドが重ならず
                    # 「ガイドに合わせる」も使えない）。タイムライン上の位置のままの対応で続け、理由を残す。
                    # キャッシュには書かない（次に開いたときにまた取り直す）
                    log.get().error("ガイドとの対応付けに失敗したので、位置のままの対応で続ける: %s", e)
                    self._alignment = fallback_alignment(self, e)
                    self._srcs["alignment"] = ("fallback", str(e))
                    _remove(keyed_alignment)
                    if publish:
                        _remove(a_cache)
                else:
                    tmp = _tmp_name(keyed_alignment)
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(self._alignment.to_json(), f, ensure_ascii=False)
                    replace_file(tmp, keyed_alignment)
                    self._srcs["alignment"] = _src_sig(keyed_alignment)
                    if publish:
                        self._publish_cache(keyed_alignment, a_cache)
            self.analysis["alignment"] = dict(self._alignment.summary(), cache=a_cache)
            advance(0.82)
            # 発音の頭（「ガイドに合わせる」のタイミングの単位）も先に取っておく
            enter("onsets")
            self.onsets("take")
            self.onsets("guide")
            self._prune_guide_cache("analysis")
            self._prune_guide_cache("alignment")
            advance(0.9)

        # ---- 歌詞があれば音素の強制アラインメント（段階2）
        for role in ("take", "guide"):
            advance(0.92 if role == "take" else 0.96)
            if role == "guide" and (not with_guide or self.guide is None):
                continue
            if not self.has_lyrics(role):
                self.analysis.pop("phonemes_%s" % role, None)
                continue
            enter("phonemes")
            try:
                self.analyze_phonemes(role, force=force)
            except Exception as e:                       # noqa: BLE001
                # 重みが無い／辞書が無いは**黙って飛ばさない**（原因が分かる形で上げる）
                from ..phoneme.hubertfa import AlignerError
                log.get().error("%s の音素アラインに失敗: %s", role, e)
                self.analysis["phonemes_%s" % role] = {"supported": False, "error": str(e)}
                if isinstance(e, AlignerError):
                    persist()
                    raise

        advance(1.0)
        persist()
        return self.analysis

    def _save_analysis(self):
        """analyze の書き込み。解析している間に別のエンジン（Claude Code・画面）が project.json を書いていたら、
        上書きせずに、読み直した内容へ解析の要約と歌詞の推定を足して書く（`merge_background` と同じ規則）。"""
        with dir_lock(self.dir):
            if not self._changed_on_disk():
                self.save()
                return
            log.get().warning("解析の間に project.json が書き換わっていたので、読み直して解析の結果を足す: %s",
                              self.json_path)
            Project(self.dir).load().merge_background(self, guide=True)
            self.load()

    def merge_background(self, q, guide=False, before_write=None):
        """裏の準備（`q`: 別のインスタンス、`background=True`）の結果を、このプロジェクト（ディスクから
        読み直したばかりのもの）に合わせて入れる。書くのはテイク側の解析の要約と、歌詞の自動推定だけ。

        - 準備の間に表や外部が書いた編集・歌詞はそのまま（ここで読み直した内容が正）
        - 歌詞の自動推定は、まだ推定していないときだけ入れる。その間に入った歌詞と重なる区間は捨てる
        - ガイド側（ガイドの F0・対応付け）は鍵付きの保存に置いてあるので、ここでは書かない
          （project.json のガイドの写しは、トラックを選んだときに合わせる）。guide=True（表の analyze が
          書き換えに出会った）なら、ガイドが同じときだけガイド側の要約も入れる
        before_write: 書く直前に呼ぶ（裏の準備が、取り消し・作業場所の破棄をもう一度確かめる。例外で書かない）。
        素材が変わっていたら何もしない（False）。足すものが無ければ（解析した時刻だけが違う）書かない。"""
        if not M.same_clip(self.take, q.take):
            return False
        before_analysis = json.loads(json.dumps(self.analysis))
        before = self._analysis_state()
        if "auto_lyrics" in q.analysis and "auto_lyrics" not in self.analysis:
            existing = self.lyrics_entries("take")
            keep = [dict(e) for e in q._auto_proposed
                    if not any(o["start_sec"] is None or
                               (o["start_sec"] < e["end_sec"] and o["end_sec"] > e["start_sec"])
                               for o in existing)]
            if keep:
                self._store_lyrics(LY.normalize(existing + keep), "take", save=False)
            self.analysis["auto_lyrics"] = dict(q.analysis["auto_lyrics"], entries=len(keep))
        if q.analysis.get("take"):
            self.analysis["take"] = dict(q.analysis["take"])
        if LY.key_of(self.lyrics_entries("take")) == LY.key_of(q.lyrics_entries("take")) \
                and q.analysis.get("phonemes_take"):
            self.analysis["phonemes_take"] = dict(q.analysis["phonemes_take"])
        if guide and q.guide is not None and M.same_clip(self.guide, q.guide):
            for k in ("guide", "alignment"):
                if q.analysis.get(k):
                    self.analysis[k] = dict(q.analysis[k])
            if LY.key_of(self.lyrics_entries("guide")) == LY.key_of(q.lyrics_entries("guide")) \
                    and q.analysis.get("phonemes_guide"):
                self.analysis["phonemes_guide"] = dict(q.analysis["phonemes_guide"])
        if self._analysis_state() == before and self._json_sig() == self._disk_sig:
            self.analysis = before_analysis
            return True
        if before_write is not None:
            before_write()
        self.save()
        return True

    def _analysis_state(self):
        """analyze・merge_background が書き換えるもの（解析の要約・歌詞）を、解析した時刻を除いて比べる形に。"""
        return json.dumps([_stable(self.analysis), self.lyrics], sort_keys=True, ensure_ascii=False,
                          default=str)

    def analysis_cached(self, estimator=None, sweep=False):
        """analyze（既定の設定）が**キャッシュを読むだけで済む**か（重い計算・読み込みが無い）。
        estimator を省くと `f0_estimator()`。保存したテイクの解析が別の方式のものなら False。

        analyze_take はこのときジョブにせず、裏の準備にも合流せずにすぐ返す（issue #63）。見るもの:
        テイクの解析・歌詞の推定（済みか、推定できない）・ガイドの解析と対応付け（鍵付きの保存）・
        発音の頭・音素（今の歌詞の鍵付きの保存か、同じ入力で失敗したことを覚えているか）。"""
        estimator = self.f0_estimator(estimator)
        take_cache = self._cache_path("take-analysis.json")
        if not os.path.exists(take_cache):
            return False
        ta = self.analysis.get("take") or {}
        if ta.get("estimator") is not None and not f0mod.same_estimator(
                ta["estimator"], ta.get("estimator_version"), estimator):
            return False                                 # 方式を替えた: テイクから解析し直す
        if "auto_lyrics" not in self.analysis and os.environ.get("VOCAL_ENGINE_AUTO_LYRICS", "1") != "0":
            from ..phoneme.hubertfa import model_found
            if model_found():
                return False
        if self.guide is not None:
            gdir = self._guide_analysis_cache_dir(estimator, sweep)
            if not os.path.exists(os.path.join(gdir, "guide-analysis.json")):
                return False
            try:
                if self._take_f0 is None or self._srcs.get("take") != _src_sig(take_cache):
                    self._load_take_analysis(take_cache)
                adir = self._alignment_cache_dir()
            except (OSError, ValueError, KeyError, CacheBroken):
                return False                             # 壊れていた: 外して印も取り消した（合流して作り直す）
            if not os.path.exists(os.path.join(adir, "alignment.json")):
                return False                             # 対応付けがまだ・前回失敗した（#32。選ぶたびに取り直す）
            if not os.path.exists(os.path.join(gdir, "onsets.json")):
                return False
        if not os.path.exists(self._cache_path("onsets-take.json")) and self.guide is not None:
            return False
        for role in ("take", "guide"):
            if role == "guide" and self.guide is None:
                continue
            entries = self.lyrics_entries(role)
            if not entries:
                continue
            failed = self._phoneme_failed.get(role)
            if failed is not None and failed[0] == self._phoneme_input_key(role, entries):
                continue
            if self._phonemes.get(role) is not None:
                continue
            if not os.path.exists(self._keyed_phoneme_cache(role, entries)):
                return False
        return True

    def view_key(self, *extra):
        """描画データ（`view/export_data.py`）の入力の鍵（issue #63）。同じなら前に作ったものを使い回す。

        入れるもの: 素材（テイク・ガイドの参照）・歌詞・編集の履歴（changeset。有効な編集はここから決まる）・
        メモリの解析結果を読んだファイルの署名（`_srcs`）・発音の頭の鍵・音素の失敗・呼び出しの引数（extra）・
        エンジンのコードの版（`_code_sig`）。解析の要約（`analysis`）は描画データに使わないので入れない
        （対応付けの要約は、計算した直後と読み直した後で丸めが違う）。"""
        try:
            self._refresh_analysis_sources()
        except CacheBroken:
            self._forget_analysis()              # 読み直した版が壊れていた: 作り直して読み直す
            self.ensure_analyzed()
        from ..view.export_data import VIEW_DATA_VERSION
        cs = [[c.id, c.label, c.author, c.created_at, c.undone, c.discarded, c.ops] for c in self.changesets]
        # 解析のファイルはパスを入れない。写しのファイル ID は異なるため、読んだ場所により鍵は分かれる。
        srcs = sorted((k, list(v[1:]) if v and v[0] != "fallback" else v) for k, v in self._srcs.items())
        d = [VIEW_DATA_VERSION, _code_sig(), SCHEMA_VERSION, self.take, self.guide, self.lyrics, self.align_method,
             cs, srcs, sorted((r, v[0], self._onset_sigs.get(r)) for r, v in self._onsets.items()),
             [self.phoneme_error("take"), self.phoneme_error("guide")], list(extra)]
        raw = json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

    def _cache_broken(self, path, err):
        """解析のキャッシュが読めなかった: そのファイルを外し、準備済みの印と内容ハッシュの使い回しを取り消す
        （`PREP_BROKEN`）。上げる例外（`CacheBroken`）を返す。"""
        log.get().warning("解析のキャッシュが壊れている（外して準備し直す）: %s: %s", path, err)
        _remove(path)
        if PREP_BROKEN is not None:
            try:
                PREP_BROKEN(self.dir, path)
            except Exception as e:               # noqa: BLE001  印を外せなくても、ファイルは外した
                log.get().warning("準備済みの印を取り消せない: %s", e)
        return CacheBroken(path, err)

    def _read_analysis(self, path):
        """解析のキャッシュ（F0 とノート）を読む。読めなければ `CacheBroken`。"""
        try:
            d = read_json(path)
            return F0Result.from_json(d["f0"]), [_note_from_json(n) for n in d["notes"]]
        except _BROKEN_ERRORS as e:
            raise self._cache_broken(path, e) from e

    def _read_alignment(self, path):
        """対応付けのキャッシュを読む。読めなければ `CacheBroken`。"""
        try:
            return Alignment.from_json(read_json(path))
        except _BROKEN_ERRORS as e:
            raise self._cache_broken(path, e) from e

    def _load_take_analysis(self, path):
        sig = _src_sig(path)                    # 読む前に（読んでいる間に置き換わったら、次に読み直す）
        self._take_f0, self._take_notes = self._read_analysis(path)
        self._srcs["take"] = sig

    def _load_guide_analysis(self, path):
        sig = _src_sig(path)
        self._guide_f0, self._guide_notes = self._read_analysis(path)
        self._srcs["guide"] = sig

    def _refresh_analysis_sources(self):
        """メモリにある描画用の解析を、現在のファイルの版へ合わせる。"""
        self._check_audio_versions()
        for role, value, loader in (("take", self._take_f0, self._load_take_analysis),
                                    ("guide", self._guide_f0, self._load_guide_analysis)):
            src = self._srcs.get(role)
            if value is not None and src and _src_sig(src[0]) != src:
                if _src_sig(src[0]) is None:
                    if role == "take":
                        self._take_f0 = self._take_notes = None
                    else:
                        self._guide_f0 = self._guide_notes = None
                    self._srcs.pop(role, None)
                else:
                    loader(src[0])
                self._notes_cache = None
        src = self._srcs.get("alignment")
        if self._alignment is not None and src and src[0] != "fallback" and _src_sig(src[0]) != src:
            if _src_sig(src[0]) is None:
                self._alignment = None
                self._srcs.pop("alignment", None)
            else:
                sig = _src_sig(src[0])
                self._alignment = self._read_alignment(src[0])
                self._srcs["alignment"] = sig
        for role in ("take", "guide"):
            src = self._srcs.get("ph_" + role)
            if self._phonemes.get(role) is not None and src and _src_sig(src[0]) != src:
                self._phonemes[role] = None
                self._srcs.pop("ph_" + role, None)
            if role in self._onsets:
                path = (os.path.join(self._guide_cache_path, "onsets.json")
                        if role == "guide" and self._guide_cache_path else self._cache_path("onsets-%s.json" % role))
                if self._onset_sigs.get(role) != _src_sig(path):
                    self._onsets.pop(role, None)
                    self._onset_sigs.pop(role, None)

    def ensure_analyzed(self):
        # 準備済みのはずのキャッシュが壊れていた（`CacheBroken`。外して印も取り消した）: 裏の準備に合流して
        # 作り直してもらい（準備を止めていれば表で計算し）、鍵付きの保存から読み直す。壊れたファイルが
        # 複数でも、1 回ごとに 1 つ外れるので回数を限って繰り返す
        for attempt in range(4):
            try:
                if attempt == 0:
                    return self._ensure_analyzed()
                self._forget_analysis()
                with front(self.dir):
                    self.analyze()
                return self
            except CacheBroken:
                if attempt == 3:
                    raise
        return self

    def _forget_analysis(self):
        """メモリの解析（テイク・ガイド・対応付け）を捨てる（壊れたキャッシュから読み直す前）。"""
        self._take_f0 = self._take_notes = None
        self._guide_f0 = self._guide_notes = None
        self._alignment = None
        self._notes_cache = None
        for k in ("take", "guide", "alignment"):
            self._srcs.pop(k, None)

    def _ensure_analyzed(self):
        self._refresh_analysis_sources()
        if self._media_changed:
            self._media_changed = False
            self.analyze(force=True)
            return self
        if self._take_f0 is None:
            cache = self._cache_path("take-analysis.json")
            if not os.path.exists(cache) and not self.background:
                # 裏の準備がこのトラックを順番待ち・解析中なら、合流して終わりを待って使う。
                # 自分で解析するときは、その間トラックを占有する（裏が同じ計算を始めない）
                with front(self.dir) as entered:
                    if entered:
                        return self._ensure_analyzed()
            if os.path.exists(cache):
                self._load_take_analysis(cache)
                g = self._cache_path("guide-analysis.json")
                if self.guide and os.path.exists(g):
                    self._load_guide_analysis(g)
                a = self._cache_path("alignment.json")
                if self.guide and os.path.exists(a):
                    sig = _src_sig(a)
                    self._alignment = self._read_alignment(a)
                    self._srcs["alignment"] = sig
            else:
                self.analyze()
        return self

    @property
    def take_f0(self):
        self.ensure_analyzed()
        return self._take_f0

    @property
    def guide_f0(self):
        self.ensure_analyzed()
        return self._guide_f0

    @property
    def take_notes(self):
        """テイクのノート（解析の結果に、編集リストの分割 `split` / 結合 `merge` を当てたもの）。"""
        self.ensure_analyzed()
        return self._notes_with_edits()

    def _notes_with_edits(self):
        base = self._take_notes or []
        ops = [e for e in self.edits if e.kind in ("split", "merge")]
        if not ops:
            return base
        key = tuple(e.id for e in ops)
        c = self._notes_cache
        if c is not None and c[0] is base and c[1] == key:
            return c[2]
        from .notes_edit import apply_note_edits
        ns = apply_note_edits(base, ops, self._take_f0)
        self._notes_cache = (base, key, ns)
        return ns

    @property
    def analysis_notes(self):
        """解析そのままのノート（分割・結合を当てる前）。"""
        self.ensure_analyzed()
        return self._take_notes or []

    @property
    def guide_notes(self):
        self.ensure_analyzed()
        sg = self.score_guide_notes()
        if sg is not None:
            return sg
        return self._guide_notes or []

    def score_guide(self):
        """ガイドが譜面から作った合成音（`score_guide.write` の印がある）なら、その印。無ければ None。"""
        g = self.guide
        if not g or not g.get("path"):
            return None
        from .. import score_guide as SG
        return SG.read(g["path"])

    def score_guide_notes(self):
        """譜面ガイドのノート（ガイドの秒）。音から分割したノートの代わりに使う（同じ高さが続くノート・
        1 半音の短い音も譜面どおりに分かれる）。譜面ガイドでなければ None。"""
        d = self.score_guide()
        if d is None:
            return None
        g = self.guide
        key = (id(d), int(g.get("offset_frames") or 0), int(g["frames"]))
        if getattr(self, "_sg_notes", None) is not None and self._sg_notes[0] == key:
            return self._sg_notes[1]
        from .. import score_guide as SG
        ns = SG.guide_notes(d, offset_frames=int(g.get("offset_frames") or 0), sr=int(g["sr"]),
                            duration_sec=float(g["frames"]) / float(g["sr"]))
        self._sg_notes = (key, ns)
        return ns

    @property
    def alignment(self):
        self.ensure_analyzed()
        return self._alignment

    def _note_quick(self, note_id):
        """編集の範囲を引く用の `note()`。解析を読み込み済みなら、ファイルの版を確かめ直さずに引く
        （編集リストの範囲を求めるたびに音声ファイルの版を確かめていて、編集の多いトラックで 1 回の
        再合成の前に stat が 1000 回を超えていた。クラウドの仮想ドライブで 1 秒以上）。版の確かめは
        呼び出し元のツールの頭（`ensure_analyzed`）で済んでいる。"""
        if self._take_f0 is None:
            return self.note(note_id)
        ns = self._notes_with_edits()
        idx = getattr(self, "_note_index", None)
        if idx is None or idx[0] is not ns:
            idx = self._note_index = (ns, {n.id: n for n in ns})
        n = idx[1].get(note_id)
        if n is None:
            return self.note(note_id)
        return n

    def note(self, note_id):
        for n in self.take_notes:
            if n.id == note_id:
                return n
        raise ProjectError("ノートが無い: %s（list_notes で確認）" % note_id)

    def deviations(self, threshold_cents=None, threshold_ms=None):
        if self.guide is None:
            raise ProjectError("ガイドが設定されていない（open_project の guide_path）")
        self.ensure_analyzed()
        if self._alignment is None:
            raise ProjectError("ガイドとの対応付けがまだ（analyze_take を実行する）")
        g2t, basis = self.guide_to_take()
        dtw = basis["basis"] == "dtw"
        devs, meta = deviations(self.take_notes, self._take_f0,
                                self.guide_notes, self._guide_f0, self._alignment,
                                guide_to_take=None if dtw else g2t,
                                offset_sec=None if dtw else basis["offset_ms"] / 1000.0)
        meta["basis"] = basis["basis"]
        if threshold_cents is not None or threshold_ms is not None:
            tc = threshold_cents if threshold_cents is not None else 1e18
            tm = threshold_ms if threshold_ms is not None else 1e18
            devs = [d for d in devs
                    if (d.pitch_cents is not None and abs(d.pitch_cents) >= tc)
                    or (d.timing_ms is not None and abs(d.timing_ms) >= tm)]
        return devs, meta

    def boundary_deviations(self, threshold_ms=None):
        """音素境界ごとのタイミングのずれ（段階2）。"""
        if self.guide is None:
            raise ProjectError("ガイドが設定されていない（open_project の guide_path）")
        self.ensure_analyzed()
        tp = self.phonemes("take")
        if tp is None:
            raise ProjectError("テイクに歌詞が無い（set_lyrics → analyze_take）")
        g2t, basis = self.guide_to_take()
        dtw = basis["basis"] == "dtw"
        devs, meta = boundary_deviations(tp, self.phonemes("guide"), self.guide_notes,
                                         self._alignment, guide_to_take=None if dtw else g2t,
                                         offset_sec=None if dtw else basis["offset_ms"] / 1000.0)
        meta["basis"] = basis["basis"]
        if threshold_ms is not None:
            devs = [d for d in devs
                    if d.timing_ms is not None and abs(d.timing_ms) >= threshold_ms]
        return devs, meta

    # ------------------------------------------------------------ 編集後の時間
    def time_map(self):
        """編集前の秒 → 編集後の秒（区分線形）。`render/pipeline.py` と同じ規則。"""
        from ..render.pipeline import edits_to_segments
        from ..view.export_data import build_time_map
        segs = edits_to_segments(self.edits, self.edit_span)
        return build_time_map(segs, 0.0, max(self.duration_sec, 1e-6))

    def edited_sec(self, t):
        src, out = self.time_map()
        return float(np.interp(float(t), src, out))

    def edited_length(self, t0, t1):
        src, out = self.time_map()
        a, b = np.interp([float(t0), float(t1)], src, out)
        return float(b - a)

    # ------------------------------------------------------------ 編集リスト
    def _next_id(self, kind):
        self._seq[kind] = self._seq.get(kind, 0) + 1
        return ("e%03d" if kind == "edit" else "c%03d") % self._seq[kind]

    def _replay(self):
        """取り消されていない changeset を順に適用して、有効な編集リストを作る。"""
        by_id = {}
        order = []
        for cs in self.changesets:
            if cs.undone:
                continue
            for op in cs.ops:
                if op.get("op") == "add":
                    e = Edit.from_json(op["edit"])
                    by_id[e.id] = e
                    # 置き換え（分割で範囲対象に直す・オリジナルに戻すで切る等）は元の編集の位置へ入れる。
                    # 編集リストの順は意味を持つ（pitch_curve は後勝ち、鉛筆は「後から入ったピッチ編集」
                    # だけを描いた線に足す）ので、末尾へ回すと音が変わる
                    at = op.get("in_place_of")
                    if at and at in order:
                        order.insert(order.index(at), e.id)
                    else:
                        order.append(e.id)
                elif op.get("op") == "remove":
                    by_id.pop(op["edit_id"], None)
        seen = set()
        self.edits = []
        for eid in order:
            if eid in by_id and eid not in seen:
                seen.add(eid)
                self.edits.append(by_id[eid])
        return self.edits

    def apply_edits(self, specs, author="ai", label=None, origin="manual"):
        """specs = [{kind, target, params, note}] を 1 つの changeset として適用。

        origin: "auto"（ガイドに合わせる）/ "manual"。変わったノートに印を付ける（`project/correction.py`）。"""
        self.reload_if_changed()
        from .correction import Marker, kinds_of, TIMING_KINDS
        marker = Marker(self, kinds_of(self, specs), origin)
        cs_id = self._next_id("changeset")
        ops = []
        made = []
        for s in specs:
            tgt = s["target"] if isinstance(s["target"], Target) else Target.from_json(s["target"])
            e = Edit(id=self._next_id("edit"), kind=s["kind"], target=tgt,
                     params=dict(s.get("params", {})), author=author,
                     changeset=cs_id, note=s.get("note"))
            ops.append({"op": "add", "edit": e.to_json()})
            made.append(e)
        cs = Changeset(id=cs_id, label=label or (made[0].describe() if made else "（空）"),
                       author=author, created_at=now_iso(), ops=ops)
        self.changesets.append(cs)
        self._replay()
        mark = marker.op()
        if mark:
            cs.ops.append(mark)
        if (origin == "manual" and mark and
                any(s.get("kind") in TIMING_KINDS for s in specs)):
            by_id = {n.id: n for n in self.take_notes}
            spans = [[by_id[i].start_sec, by_id[i].end_sec]
                     for i in mark["timing"] if i in by_id]
            if spans:
                cs.ops.append({"op": "estimated_scope", "spans": spans})
        self.save()
        log.get().info("changeset %s: %s（編集 %d 件）", cs_id, cs.label, len(made))
        return cs, made

    def apply_changes(self, remove_ids, specs, author="ai", label=None, origin="manual"):
        """外す編集と入れる編集を **1 つの changeset** にする（undo で両方まとめて戻る）。

        `project/timing.py` の組み直しで使う（変わる範囲のタイミング編集を外して、
        正規形の stretch / crop / silence を入れ直す）。origin は `apply_edits` と同じ。"""
        from .correction import Marker, kinds_of, TIMING_KINDS
        structural = []
        for spec in specs:
            if spec.get("kind") not in ("split", "merge"):
                continue
            target = spec["target"]
            t = float(target.start_sec if isinstance(target, Target) else target["start_sec"])
            touched = [n for n in self.take_notes if n.start_sec - 1e-6 <= t <= n.end_sec + 1e-6]
            if spec["kind"] == "split":
                touched = [n for n in touched if n.start_sec + 1e-6 < t < n.end_sec - 1e-6]
            structural.extend([n.start_sec, n.end_sec] for n in touched)
        marker = Marker(self, kinds_of(self, specs, remove_ids), origin)
        live = {e.id for e in self.edits}
        cs_id = self._next_id("changeset")
        ops = [{"op": "remove", "edit_id": i} for i in remove_ids if i in live]
        made = []
        for s in specs:
            tgt = s["target"] if isinstance(s["target"], Target) else Target.from_json(s["target"])
            e = Edit(id=self._next_id("edit"), kind=s["kind"], target=tgt,
                     params=dict(s.get("params", {})), author=author,
                     changeset=cs_id, note=s.get("note"))
            op = {"op": "add", "edit": e.to_json()}
            if s.get("in_place_of"):
                op["in_place_of"] = s["in_place_of"]     # 編集リストの中の元の位置に入れる
            ops.append(op)
            made.append(e)
        cs = Changeset(id=cs_id, label=label or (made[0].describe() if made else "（空）"),
                       author=author, created_at=now_iso(), ops=ops)
        self.changesets.append(cs)
        self._replay()
        mark = marker.op()
        if mark:
            cs.ops.append(mark)
        scopes = list(structural)
        if (origin == "manual" and mark and
                any(s.get("kind") in TIMING_KINDS for s in specs)):
            by_id = {n.id: n for n in self.take_notes}
            scopes.extend([by_id[i].start_sec, by_id[i].end_sec]
                          for i in mark["timing"] if i in by_id)
        if scopes:
            cs.ops.append({"op": "estimated_scope", "spans": scopes})
        self.save()
        log.get().info("changeset %s: %s（外す %d / 入れる %d）", cs_id, cs.label,
                       len(ops) - len(made), len(made))
        return cs

    def remove_edits(self, edit_ids, author="ai", label=None):
        self.reload_if_changed()
        ids = [e for e in edit_ids if any(x.id == e for x in self.edits)]
        from .correction import Marker, kinds_of
        marker = Marker(self, kinds_of(self, [], ids))
        cs_id = self._next_id("changeset")
        cs = Changeset(id=cs_id, label=label or ("編集 %d 件を取り消し" % len(ids)),
                       author=author, created_at=now_iso(),
                       ops=[{"op": "remove", "edit_id": i} for i in ids])
        self.changesets.append(cs)
        self._replay()
        mark = marker.op()
        if mark:
            cs.ops.append(mark)
        self.save()
        return cs, ids

    def undo(self, changeset_id=None):
        self.reload_if_changed()
        target = None
        if changeset_id:
            for c in self.changesets:
                if c.id == changeset_id:
                    target = c
                    break
            if target is None:
                raise ProjectError("changeset が無い: %s" % changeset_id)
            if target.undone:
                raise ProjectError("%s は既に取り消し済み" % changeset_id)
        else:
            for c in reversed(self.changesets):
                if not c.undone:
                    target = c
                    break
            if target is None:
                raise ProjectError("取り消せる変更が無い")
        before = {e.id for e, _ in self._missing_note_targets()}
        target.undone = True
        self._replay()
        lost = [(e, n) for e, n in self._missing_note_targets() if e.id not in before]
        if lost:
            # 途中の changeset（分割など）だけを取り消すと、後の編集が対象にしているノート
            # （分割の右の片など）が無くなり、再合成・書き出し・画面のデータが全部止まる
            target.undone = False
            self._replay()
            raise ProjectError("%s を取り消すと、後の編集 %s が対象にしているノート %s が無くなる。"
                               "後の変更から順に取り消すこと"
                               % (target.id, ", ".join(e.id for e, _ in lost[:5]),
                                  ", ".join(sorted({n for _, n in lost})[:5])))
        self._state_ops(target, undo=True)
        self.save()
        log.get().info("undo: %s（%s）", target.id, target.label)
        return target

    def _missing_note_targets(self):
        """ノート対象の編集のうち、そのノートが今のノート列に無いもの [(edit, note_id)]。"""
        if not any(e.target.type == "note" for e in self.edits):
            return []
        ids = {n.id for n in self.take_notes}
        return [(e, e.target.note_id) for e in self.edits
                if e.target.type == "note" and e.target.note_id not in ids]

    def _redo_target(self):
        """直近に undo した changeset = 末尾に続く undone の並びの先頭（捨てたものは飛ばす）。"""
        target = None
        for c in reversed(self.changesets):
            if c.discarded:
                continue
            if c.undone:
                target = c
            else:
                break
        return target

    def discard(self, changeset_id):
        """取り消した changeset を「捨てた」ことにする（redo で戻さない）。"""
        for c in self.changesets:
            if c.id == changeset_id:
                was = c.undone
                c.undone = True
                c.discarded = True
                self._replay()
                if not was:
                    self._state_ops(c, undo=True)
                self.save()
                return c
        raise ProjectError("changeset が無い: %s" % changeset_id)

    def find_changeset(self, changeset_id):
        for c in self.changesets:
            if c.id == changeset_id:
                return c
        return None

    def _state_ops(self, cs, undo):
        """編集リストの外の状態を持つ op（歌詞 `lyrics`）を、取り消し（before へ）／やり直し（after へ）で当てる。"""
        ops = [o for o in cs.ops if o.get("op") == "lyrics"]
        for o in (reversed(ops) if undo else ops):
            self._store_lyrics(LY.normalize(o.get("before" if undo else "after") or []),
                               o.get("source", "take"), save=False)

    def can_redo(self):
        return self._redo_target() is not None

    def redo(self, changeset_id=None):
        """undo を取り消す。省略すると末尾から順（直近に undo したもの）。
        changeset_id を渡すとその changeset（曲の履歴 `session.py` が順を持っているとき）。"""
        self.reload_if_changed()
        if changeset_id:
            target = self.find_changeset(changeset_id)
            if target is None:
                raise ProjectError("changeset が無い: %s" % changeset_id)
            if not target.undone or target.discarded:
                raise ProjectError("%s はやり直せない（取り消していない・捨てた）" % changeset_id)
        else:
            target = self._redo_target()
        if target is None:
            raise ProjectError("やり直せる変更が無い")
        target.undone = False
        self._replay()
        self._state_ops(target, undo=False)
        self.save()
        log.get().info("redo: %s（%s）", target.id, target.label)
        return target

    def edits_for(self, start_sec=None, end_sec=None):
        """範囲に関わる編集（ノート対象は実時間に解決して判定）。"""
        s = -1e18 if start_sec is None else float(start_sec)
        e = 1e18 if end_sec is None else float(end_sec)
        out = []
        for ed in self.edits:
            a, b = self.edit_span(ed)
            if b > s and a < e:
                out.append(ed)
        return out

    def edit_span(self, edit):
        if edit.target.type == "note":
            n = self._note_quick(edit.target.note_id)
            return n.start_sec, n.end_sec
        return float(edit.target.start_sec), float(edit.target.end_sec)

    # ------------------------------------------------------------ 歌詞・音素
    def set_lyrics(self, text, source="take", start_sec=None, end_sec=None,
                   mode="replace"):
        """歌詞を**区間ごと**に入れる（`phoneme/lyrics.py: upsert` の規則）。

        範囲を省くと素材全体の 1 件になる（段階2 までと同じ）。
        範囲つきで空文字を渡すとその区間の歌詞を消す。
        """
        self.reload_if_changed()
        cur = self.lyrics.get(source, [])
        ent = LY.upsert(cur, text, start_sec=start_sec, end_sec=end_sec, mode=mode)
        return self._store_lyrics(ent, source)

    def set_lyrics_entries(self, entries, source="take"):
        """区間の配列をまるごと置き換える（MCP の `set_lyrics(entries=[...])`）。"""
        self.reload_if_changed()
        return self._store_lyrics(LY.normalize(entries), source)

    def change_lyrics(self, entries, source="take", author="ai", label=None):
        """歌詞を**取り消せる変更**として入れる（changeset に `lyrics` の op: 前と後の区間の配列）。

        変わらなければ (None, 区間)。取り消し・やり直しで前／後の歌詞に戻す（`_state_ops`）。
        開くときの歌詞・ガイドの歌詞の控えからの復元は取り消しの対象にしない（`set_lyrics_entries`）。"""
        self.reload_if_changed()
        if source not in ("take", "guide"):
            raise ProjectError("source は take か guide")
        after = LY.normalize(entries)
        before = [dict(e) for e in self.lyrics.get(source, [])]
        if after == before:
            return None, after
        cs = Changeset(id=self._next_id("changeset"), label=label or "歌詞（%s）" % source,
                       author=author, created_at=now_iso(),
                       ops=[{"op": "lyrics", "source": source, "before": before,
                             "after": [dict(e) for e in after]}])
        self.changesets.append(cs)
        self._store_lyrics(after, source)
        log.get().info("changeset %s: %s", cs.id, cs.label)
        return cs, after

    def _store_lyrics(self, entries, source, save=True):
        if source not in ("take", "guide"):
            raise ProjectError("source は take か guide")
        if entries:
            self.lyrics[source] = entries
        else:
            self.lyrics.pop(source, None)
        self._phonemes[source] = None
        self._srcs.pop("ph_" + source, None)
        self.analysis.pop("phonemes_%s" % source, None)
        cache = self._cache_path("%s-phonemes.json" % source)
        if os.path.exists(cache) and not self.background:    # 写しは表のもの（裏の準備では消さない）
            try:
                os.remove(cache)
            except OSError:
                pass
        if not save:
            return entries
        self.save()
        log.get().info("%s の歌詞を設定: %d 区間 / %r",
                       source, len(entries), LY.text_of(entries)[:40])
        return entries

    def lyrics_entries(self, source="take"):
        return list(self.lyrics.get(source, []))

    def estimated_excluded_note_ids(self):
        """Notes with boundaries chosen by a user; undo removes their exclusion."""
        spans = [span for cs in self.changesets if not cs.undone and not cs.discarded
                 for op in cs.ops if op.get("op") == "estimated_scope"
                 for span in op.get("spans", [])]
        return {n.id for n in self.take_notes if any(
            min(n.end_sec, b) - max(n.start_sec, a) > 1e-5 for a, b in spans)}

    def _infer_missing_lyrics(self):
        from ..phoneme.auto_lyrics import infer, utterance_ranges
        from ..phoneme.hubertfa import model_found

        if os.environ.get("VOCAL_ENGINE_AUTO_LYRICS", "1") == "0":
            return
        if not model_found():
            return
        groups = utterance_ranges(self._take_notes, self.duration_sec)
        existing = self.lyrics_entries("take")
        missing = [(a, b) for a, b, _ in groups
                   if not any(e["start_sec"] is None or
                              (e["start_sec"] < b and e["end_sec"] > a)
                              for e in existing)]
        proposed = []
        if missing:
            # 推定の結果はテイクと区間を鍵にして残す（裏の準備が書き込む前に取り消されても、やり直さない）
            key = [AUTO_LYRICS_CACHE_VERSION, self._clip_cache_identity(self.take),
                   [[round(float(a), 4), round(float(b), 4)] for a, b in missing]]
            path = self._cache_path("auto-lyrics.json")
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                cached = d["proposed"] if d.get("key") == key else None
            except (OSError, ValueError, KeyError):
                cached = None
            if cached is not None:
                proposed = [dict(e) for e in cached]
            else:
                x, sr = self.audio("take")
                for a, b in missing:
                    syll = infer(x[int(a * sr):int(b * sr)], sr)
                    if not syll:
                        continue
                    reading = "".join(s.kana for s in syll)
                    confidence = float(np.mean([s.confidence for s in syll]))
                    proposed.append({"start_sec": a, "end_sec": b, "text": reading,
                                     "origin": "estimated", "estimate": {
                                         "reading": reading, "confidence": round(confidence, 4)}})
                tmp = _tmp_name(path)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"key": key, "proposed": proposed}, f, ensure_ascii=False)
                replace_file(tmp, path)
        self._auto_proposed = [dict(e) for e in proposed]
        if proposed:
            self._store_lyrics(LY.normalize(existing + proposed), "take", save=False)
        self.analysis["auto_lyrics"] = {"attempted": True, "entries": len(proposed)}

    def lyrics_text(self, source="take"):
        """全区間をつないだ歌詞（画面の入力欄・要約用）。"""
        return LY.text_of(self.lyrics.get(source, []))

    def has_lyrics(self, source="take"):
        return bool(self.lyrics.get(source))

    def _phoneme_input_key(self, source, entries):
        """音素アラインの入力の鍵（歌詞の区間と、音声のパス・クリップの範囲）。"""
        info = self.take if source == "take" else self.guide
        audio = None if info is None else [info.get("path"), int(info.get("offset_frames", 0) or 0),
                                           int(info.get("frames") or 0)]
        return json.dumps([LY.key_of(entries), audio], ensure_ascii=False, sort_keys=True,
                          default=str)

    def phoneme_error(self, source="take"):
        """今の歌詞・音声で音素アラインが失敗していればその理由（無ければ None）。"""
        m = getattr(self, "_phoneme_failed", {}).get(source)
        if m is None or m[0] != self._phoneme_input_key(source, self.lyrics_entries(source)):
            return None
        return str(m[1]) or type(m[1]).__name__

    def analyze_phonemes(self, source="take", force=False):
        """歌詞があれば強制アラインメントして `PhonemeResult` を返す（無ければ None）。

        区間ごとの失敗は結果の `warnings`（`align_failed`）に入り、その区間は音素なしで続く。
        全体が失敗したとき（重みが無い・音声が読めないなど）は例外を上げ、**同じ歌詞・音声では覚えておいて
        やり直さない**（`force=True`・歌詞や音声が変われば取り直す。issue #58: 操作のたびに重いアラインを
        やり直して失敗していた）。
        """
        if not self.has_lyrics(source):
            return None
        from ..phoneme.analyze import ALIGN_REVISION, align_lyrics_ranges
        from ..phoneme.model import PhonemeResult
        if not hasattr(self, "_phoneme_failed"):
            self._phoneme_failed = {}
        cache = self._cache_path("%s-phonemes.json" % source)
        entries = self.lyrics_entries(source)
        in_key = self._phoneme_input_key(source, entries)
        failed = self._phoneme_failed.get(source)
        if not force and failed is not None and failed[0] == in_key:
            raise failed[1].with_traceback(None)
        keyed = self._keyed_phoneme_cache(source, entries)
        src = self._srcs.get("ph_" + source)
        if not force and self._phonemes.get(source) is not None and src is not None and \
                _src_sig(src[0]) == src and (self.background or os.path.exists(cache)):
            # 今の歌詞の音素を、同じファイルからもう読んでいる（歌詞を変えると _phonemes は捨てる）
            self._register_phonemes(source, keyed if self.background else None)
            return self._phonemes[source]
        for path in ((cache, keyed) if not force else ()):
            if not os.path.exists(path):
                continue
            sig = _src_sig(path)
            try:
                d = read_json(path)
                if not isinstance(d, dict):
                    raise ValueError("音素のキャッシュが dict でない")
            except OSError:
                continue
            except ValueError as e:
                self._cache_broken(path, e)      # 外して印も取り消す（下で合流・計算し直す）
                continue
            key = d.get("entries_key")
            if key is None:
                key = d.get("lyrics")          # 段階2 のキャッシュ（1 本の文字列）
            try:
                same = LY.key_of(LY.normalize(key)) == LY.key_of(entries)
                al = d.get("aligner") or {}
                if same and al.get("failed_entries") and al.get("revision") != ALIGN_REVISION:
                    continue            # 失敗した区間を含む、前の版の結果: 直した版で取り直す
                result = PhonemeResult.from_json(d) if same else None
            except _BROKEN_ERRORS as e:
                self._cache_broken(path, e)
                continue
            if same:
                self._phonemes[source] = result
                self._srcs["ph_" + source] = sig
                if path != cache and not self.background:
                    _copy(path, cache)
                self._register_phonemes(source)
                return self._phonemes[source]
        if not self.background:
            # 裏の準備が同じトラックを順番待ち・準備中なら、合流して待ってから鍵付きの保存を見直す。
            # 自分で計算するときは、その間トラックを占有する（歌詞を変えた後に裏と表で 2 回計算しない）
            with front(self.dir, wait=not force) as entered:
                if entered:
                    return self.analyze_phonemes(source, force=force)
        x, sr = self.audio(source)
        f0r = self.take_f0 if source == "take" else self.guide_f0
        info = self.take if source == "take" else self.guide
        try:
            res = align_lyrics_ranges(x, sr, entries, f0r=f0r, source=source,
                                      duration_sec=float(info["duration_sec"]))
        except Exception as e:                           # noqa: BLE001
            self._phoneme_failed[source] = (in_key, e)
            log.get().error("%s の音素アラインに失敗（同じ歌詞・音声ではやり直さない）: %s", source, e)
            raise
        self._phoneme_failed.pop(source, None)
        if res.aligner.get("failed_entries"):
            log.get().warning("%s の音素: 歌詞の区間 %s を切れなかった（音素なしで続ける）",
                              source, res.aligner["failed_entries"])
        self._phonemes[source] = res
        if self.background:
            # 裏の準備: 鍵付きの保存だけに書く（今の写しは、トラックを選んだときの analyze が写す）
            tmp = _tmp_name(keyed)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(res.to_json(), entries_key=entries), f, ensure_ascii=False)
            replace_file(tmp, keyed)
            self._srcs["ph_" + source] = _src_sig(keyed)
            self._prune_phoneme_cache(source)
            self._register_phonemes(source, keyed)
            return res
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(dict(res.to_json(), entries_key=entries), f, ensure_ascii=False)
        self._srcs["ph_" + source] = _src_sig(cache)
        _copy(cache, keyed)
        self._prune_phoneme_cache(source)
        self._register_phonemes(source, cache)
        return res

    PHONEME_CACHE_KEEP = 8          # 歌詞ごとの音素のキャッシュを残す数（取り消しで戻す用）

    def _keyed_phoneme_cache(self, source, entries):
        import hashlib
        h = hashlib.sha1(json.dumps(LY.key_of(entries), ensure_ascii=False, sort_keys=True,
                                    default=str).encode("utf-8")).hexdigest()[:12]
        return os.path.join(self.sub(os.path.join("cache", "phonemes")), "%s-%s.json" % (source, h))

    def _prune_phoneme_cache(self, source):
        d = os.path.join(self.dir, "cache", "phonemes")
        try:
            files = sorted((os.path.join(d, f) for f in os.listdir(d)
                            if f.startswith(source + "-")), key=os.path.getmtime, reverse=True)
        except OSError:
            return
        for f in files[self.PHONEME_CACHE_KEEP:]:
            _remove(f)

    def _register_phonemes(self, source, cache=None):
        r = self._phonemes.get(source)
        if r is None:
            return
        self.analysis["phonemes_%s" % source] = {
            "supported": True, "lyrics": r.lyrics, "kana": r.kana,
            "n_phonemes": len([p for p in r.phonemes if p.label != "silence"]),
            "n_boundaries": len(r.boundaries),
            "confidence": round(r.confidence, 3),
            "entries": len(r.entries) or 1,
            "aligner": r.aligner.get("name"), "rtf": r.aligner.get("rtf"),
            "warnings": len(r.warnings),
            "failed_entries": list(r.aligner.get("failed_entries") or []),
            "cache": cache or self._cache_path("%s-phonemes.json" % source),
        }

    def phonemes(self, source="take"):
        """解析済みの音素（未解析なら歌詞があるときだけ走らせる）。

        アラインが失敗したら None（**例外にしない**。画面の描画・再生・戻す・ガイドに合わせるは音素なしで
        続ける。理由は `phoneme_error()`）。失敗は覚えておくので、呼ぶたびにやり直さない（issue #58）。"""
        if self._phonemes.get(source) is None and self.has_lyrics(source):
            try:
                self.analyze_phonemes(source)
            except Exception:                            # noqa: BLE001  ログは analyze_phonemes が 1 回だけ出す
                return None
        return self._phonemes.get(source)

    def phoneme(self, phoneme_id, source="take"):
        r = self.phonemes(source)
        if r is None:
            raise ProjectError("音素がまだ無い（set_lyrics → analyze_take）")
        try:
            return r.phoneme(phoneme_id)
        except KeyError as e:
            raise ProjectError(str(e))

    def boundary(self, boundary_id, source="take"):
        r = self.phonemes(source)
        if r is None:
            raise ProjectError("音素がまだ無い（set_lyrics → analyze_take）")
        try:
            return r.boundary(boundary_id)
        except KeyError as e:
            raise ProjectError(str(e))


def _shift_guide_lyrics(old, new, entries):
    """同じガイドの切り出す位置だけが変わった（セッションでトラックの位置をずらした）ときの、
    ガイドの歌詞の区間。ガイド内の秒は `新 = 旧 + (旧の開始 − 新の開始)`。範囲の外に出た区間は捨てる。
    別の素材・範囲なしの歌詞（素材全体）のときは None（今までどおり捨てる）。"""
    if not entries or not old or not new:
        return None
    if old.get("sha256") != new.get("sha256") or int(old.get("sr") or 0) != int(new.get("sr") or 0):
        return None
    d = (int(old.get("offset_frames") or 0) - int(new.get("offset_frames") or 0)) / float(new["sr"])
    dur = float(new["duration_sec"])
    out = []
    for e in entries:
        if e.get("start_sec") is None:
            return None
        a, b = float(e["start_sec"]) + d, float(e["end_sec"]) + d
        if a < 0 or b > dur:
            continue
        out.append(dict(e, start_sec=round(a, 4), end_sec=round(b, 4)))
    return LY.normalize(out) or None


def _tmp_name(path):
    """書きかけのファイルの名前（表と裏の準備が同じファイルを同時に書いても、途中のものを取り違えない）。"""
    return "%s.%d-%d.tmp" % (path, os.getpid(), threading.get_ident())


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _copy(src, dst):
    """写す（更新時刻も元に合わせる。ファイル ID は写しごとに異なる）。"""
    import shutil
    try:
        st = os.stat(src)
        tmp = _tmp_name(dst)
        shutil.copyfile(src, tmp)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, dst)
    except OSError:
        pass


def _default_project_dir(clip, sha=None):
    """`<projects>/<名前>-<中身のハッシュ 8 桁>[-o<オフセット>-n<長さ>]`。

    ファイル全体なら段階 2 までと同じ名前（既存のプロジェクトをそのまま開ける）。"""
    import soundfile as sf
    if clip.is_samples:
        s = clip.source
        stem = M.safe_name(s.name or s.source_id or clip.source_id or "samples", "samples")
        key = M.samples_key(clip)
        sr, total = int(s.sr), int(s.array().shape[0])
    else:
        path = os.path.abspath(clip.source)
        stem = M.safe_name(os.path.splitext(os.path.basename(path))[0])
        key = sha[:8]
        info = sf.info(path)
        sr, total = int(info.samplerate), int(info.frames)
    off, n = M.resolve_range(clip, sr, total)
    return os.path.join(projects_root(), "%s-%s%s" % (stem, key, M.range_suffix(off, n, total)))
