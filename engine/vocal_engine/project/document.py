# -*- coding: utf-8 -*-
"""プロジェクトのファイル（`.gliss`）と作業場所（issue #33。DAW と同じファイル操作）。

## 形

| もの | 置き場 | 中身 |
|---|---|---|
| **プロジェクトのファイル** `<名前>.gliss` | ユーザーが選んだ場所（既定は最初のトラックの音声の隣） | 1 つの JSON。セッション（トラック・ガイド・テンポ・取り消しの履歴）と、各トラックの編集（`project.json` の中身から、解析の要約・置き場・更新日を除いたもの）。**音声は参照**（絶対パスと、ファイルからの相対パス） |
| **作業場所** | `%LOCALAPPDATA%\\Gliss\\work\\<ファイルのパスのハッシュ>-<名前>\\`（`VOCAL_ENGINE_WORK_DIR` で変えられる） | 今までのプロジェクトのディレクトリと同じもの（`session.json`・`project.json`・`tracks/`・**解析のキャッシュ**・再生用の音）。エンジンは編集のたびにここへ書く |
| 目印 | 作業場所の `document.json` | どのファイルの作業場所か・最後に保存（読み込み）したときの中身のハッシュ |

- **保存** = 作業場所の中身を `.gliss` に書く（一時ファイルに書いて置き換える）。**未保存** = 作業場所の中身が、最後に保存した
  中身と違う（ハッシュ。編集対象の切り替え・解析の結果・ガイドの写しなど、開くたびに変わるものは比べない）。
- **クラッシュに備えた退避** = 作業場所そのもの（編集のたびに書いている）。保存しないまま落ちても、同じファイルを開けば
  作業場所の続きから開く（`recovered`）。「保存しない」で閉じたときは作業場所をファイルの中身に戻す（`close(discard=True)`）。
- 画面と Claude Code（別のプロセス）は、同じファイルを開けば同じ作業場所を使う。トラックの保存競合は失敗として返す。
- **無題**（新規プロジェクト）は `work/untitled-<時刻>-<乱数>/`。保存するとファイルの作業場所へ移る（解析のキャッシュも写す）。
- **旧形式**（`projects/<テイク名>-<sha8>/`。issue #33 より前）はそのまま開ける。そのディレクトリがそのまま作業場所で、
  編集のたびに自動で保存される（今までどおり。未保存にはならない）。名前を付けて保存すると `.gliss` になり、
  旧形式のディレクトリには `moved_to.json` を置く（同じテイクを開き直すと `.gliss` の方を開く）。

## 音声のパス

各トラック（と取り消しの履歴のスナップショット、各トラックの編集のテイク・ガイド）の `path` に、ファイルの置き場からの
相対パス `rel` を添えて書く。開くときは **絶対パス → 相対パス → ファイルと同じフォルダ（と `Media/`）の同じ名前**
の順に探す（プロジェクトと音声を一緒に別の場所・別の PC へ移しても開ける）。見つからないものは `missing` で返す
（トラックはそのまま。音声が戻れば使える）。中身のハッシュ（`sha256`）は持っているが、開くときには比べない（大きい
WAV を毎回読まない）。
"""
import copy
import hashlib
import json
import os
import shutil
import threading
import time
import uuid

from .. import log
from .. import media as M
from .session import SESSION_FILE, Session, SessionError
from .store import FileLock, ProjectError, replace_file

FORMAT = "gliss-project"
VERSION = 1
EXT = ".gliss"
MARKER = "document.json"
MOVED = "moved_to.json"
WORK_ENV = "VOCAL_ENGINE_WORK_DIR"
KINDS = ("gliss", "untitled", "legacy")
AUDIO_EXTS = (".wav", ".wave", ".bwf", ".flac", ".aif", ".aiff")

# 開くたびに変わる（ユーザーの編集ではない）もの: 未保存の判定で比べない
_VOLATILE_SESSION = ("current",)
_VOLATILE_PROJECT = ("dir", "updated_at", "analysis", "guide")


class DocumentError(ProjectError):
    pass


# ---------------------------------------------------------------- 置き場
def work_root():
    v = os.environ.get(WORK_ENV)
    if v:
        return os.path.abspath(v)
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "Gliss", "work")


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def work_dir_for(path):
    """`.gliss` のパス → 作業場所（パスで決まる。同じファイルを開けば画面と Claude Code が同じ場所を使う）。"""
    h = hashlib.sha1(_norm(path).encode("utf-8")).hexdigest()[:12]
    stem = M.safe_name(os.path.splitext(os.path.basename(path))[0])[:40]
    return os.path.join(work_root(), "%s-%s" % (h, stem))


RESET_RELEASE_SEC = 10.0          # 作業場所を空にする前に、裏の準備が離れるのを待つ最長の秒


def _release_prep(wd, timeout=None):
    """裏の準備（issue #63）がこの作業場所のトラックを解析していれば、やめさせて離れるのを待つ。
    離れた（初めから中にいない）なら True、時間切れなら False。"""
    from . import store
    if store.PREP_RELEASE is None:
        return True
    left = store.PREP_RELEASE(wd) if timeout is None else store.PREP_RELEASE(wd, timeout=timeout)
    return left is not False


# 後回しにした削除の予定（issue #63）。作業場所の置き場の `.pending-delete/<鍵>.json` に、作業場所のパスと
# **世代**（予定を立てたときの乱数）を残し、同じ世代を作業場所の中の `PENDING_TOKEN` にも書く。
# 作業場所を使い始めたら（`claim_work`。開く・読み込む・保存で移る）予定と中の世代を消す。片付け
# （`cleanup_pending`）は、予定と中の世代が同じで、ロックがすべて取れたときだけ消す。予定の読み書きと片付けは
# `<鍵>.lock` で互いに締め出す（開き直しと片付けが同時に走っても、使い始めた作業場所を消さない）
PENDING_TOKEN = ".pending-delete"
PENDING_LOCK_SEC = 10.0


def _pending_path(wd):
    key = hashlib.sha256(_norm(wd).encode("utf-8")).hexdigest()[:24]
    return os.path.join(work_root(), ".pending-delete", key + ".json")


def _pending_lock(wd):
    return FileLock(_pending_path(wd)[:-len(".json")] + ".lock")


def _token_of(wd):
    try:
        with open(os.path.join(wd, PENDING_TOKEN), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def claim_work(wd):
    """作業場所を使い始める（開く・読み込む・保存で移る）: 後回しにした削除の予定があれば取り消す。
    取り消したら True。"""
    marker = _pending_path(wd)
    token = os.path.join(wd, PENDING_TOKEN)
    if not os.path.exists(marker) and not os.path.exists(token):
        return False
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    lk = _pending_lock(wd)
    if not lk.acquire(PENDING_LOCK_SEC):
        raise DocumentError("作業場所の片付けが終わらない（%s）。少し待ってからやり直す" % wd)
    try:
        had = os.path.exists(marker)
        for path in (token, marker):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    finally:
        lk.release()
    if had:
        log.get().info("後回しにしていた作業場所の削除を取り消した（開き直した）: %s", wd)
    return had


def _work_is_safe(wd):
    root = os.path.realpath(work_root())
    target = os.path.realpath(wd)
    try:
        return os.path.commonpath((root, target)) == root and target != root
    except ValueError:  # 別のドライブ
        return False


def _try_remove_work(wd, gen):
    """ほかのエンジンが持つ作業場所・準備のロックを確認し、予定の世代（gen）が作業場所の中の世代と同じときだけ
    消す（開き直した作業場所は、その瞬間にロックが取れても消さない）。予定のロック（`_pending_lock`）の中で呼ぶ。
    消した・もう無いなら True。"""
    if not _work_is_safe(wd):
        return False
    if not os.path.exists(wd):
        return True
    locks = []
    try:
        for root, _dirs, files in os.walk(wd):
            for name in ("project.lock", "prep.lock"):
                if name not in files:
                    continue
                lk = FileLock(os.path.join(root, name))
                if not lk.try_acquire():
                    return False
                locks.append(lk)
        if not gen or _token_of(wd) != gen:
            return False                         # 開き直した（予定が取り消された・別の予定）
    except OSError:
        return False
    finally:
        for lk in locks:
            lk.release()                         # 握ったままのロックのファイルは消せない（Windows）
    # ここから消し終わるまでの開き直しは、予定のロック（呼び手が握る）で `claim_work` が待つ
    try:
        shutil.rmtree(wd)
    except OSError:
        return False
    return not os.path.exists(wd)


def cleanup_pending():
    """起動時とワーカー終了後に、永続化した削除予定を片付ける。予定の世代と作業場所の中の世代が違えば
    （開き直した）消さずに予定だけ外す。"""
    pending = os.path.join(work_root(), ".pending-delete")
    if not os.path.isdir(pending):
        return
    for name in os.listdir(pending):
        if not name.endswith(".json"):
            continue
        marker = os.path.join(pending, name)
        try:
            with open(marker, encoding="utf-8") as f:
                d = json.load(f)
            wd = d["path"]
            if marker != _pending_path(wd) or not _work_is_safe(wd):
                continue
            lk = _pending_lock(wd)
            if not lk.try_acquire():
                continue                         # 開き直している・ほかのエンジンが片付けている
            try:
                with open(marker, encoding="utf-8") as f:
                    gen = json.load(f).get("gen")   # ロックの中で読み直す
                if os.path.exists(wd) and (not gen or _token_of(wd) != gen):
                    os.remove(marker)            # 開き直した（前の版の予定は世代が無いので消さない）
                    log.get().info("作業場所は使われているので、削除の予定を外した: %s", wd)
                elif _try_remove_work(wd, gen):
                    os.remove(marker)
            finally:
                lk.release()
        except (OSError, ValueError, KeyError, TypeError):
            continue


def remove_work(wd):
    """作業場所を消す（無題を保存しないで閉じた・保存して移った）。ログがそこに開いていれば既定の場所へ戻してから
    （Windows は開いているファイルを消せない）。作業場所の置き場の外は消さない。

    裏の準備が段の途中（F0・DTW。数秒）でまだ中にいれば、ここでは消さず、離れたときに消す（エンジンのロックを
    握ったまま長く待たない）。戻り値は "removed" / "deferred" / False。"""
    if not _work_is_safe(wd):
        return False
    cur = log.current_log_file()
    if cur and _norm(cur).startswith(_norm(wd) + os.sep):
        log.set_log_file(log._DEFAULT_LOG)
    marker = _pending_path(wd)
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    gen = uuid.uuid4().hex
    lk = _pending_lock(wd)
    if not lk.acquire(PENDING_LOCK_SEC):
        return False
    try:
        if os.path.isdir(wd):
            tmp = os.path.join(wd, PENDING_TOKEN + ".%s.tmp" % uuid.uuid4().hex)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(gen)
            replace_file(tmp, os.path.join(wd, PENDING_TOKEN))
        tmp = marker + ".%s.tmp" % uuid.uuid4().hex
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"path": os.path.abspath(wd), "gen": gen}, f)
        replace_file(tmp, marker)
    finally:
        lk.release()
    from . import store
    if not _release_prep(wd):
        if store.PREP_AFTER_LEAVE is not None:
            store.PREP_AFTER_LEAVE(wd, cleanup_pending)
        return "deferred"
    if not lk.try_acquire():
        return "deferred"
    try:
        if _try_remove_work(wd, gen):
            os.remove(marker)
            return "removed"
    finally:
        lk.release()
    return "deferred"


def reset_work(wd):
    """作業場所を空にする（`backup/` は残す）。別の中身を展開する前に、前の中身の解析のキャッシュが残って
    使われないように（同じ `.gliss` のパスに別のプロジェクトを保存し直した・ファイルが外で書き換わった）。

    裏の準備が `RESET_RELEASE_SEC` 秒たっても離れなければ、空にせずに DocumentError（すぐ後に新しい中身を
    展開するので、離れるのを後回しにはできない）。"""
    if not os.path.isdir(wd):
        return
    cur = log.current_log_file()
    if cur and _norm(cur).startswith(_norm(wd) + os.sep):
        log.set_log_file(log._DEFAULT_LOG)
    if not _release_prep(wd, timeout=RESET_RELEASE_SEC):
        raise DocumentError("裏の準備が作業場所から離れない（%s）。少し待ってからやり直す" % wd)
    for name in os.listdir(wd):
        if name == "backup":
            continue
        q = os.path.join(wd, name)
        if os.path.isdir(q):
            shutil.rmtree(q, ignore_errors=True)
        else:
            try:
                os.remove(q)
            except OSError:
                pass


def new_untitled_dir():
    return os.path.join(work_root(), "untitled-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"),
                                                         uuid.uuid4().hex[:6]))


def read_marker(wd):
    try:
        with open(os.path.join(wd, MARKER), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                            # noqa: BLE001
        return None


def write_marker(wd, **kw):
    d = dict(read_marker(wd) or {})
    d.update(kw)
    d["format"] = "gliss-work"
    os.makedirs(wd, exist_ok=True)
    tmp = os.path.join(wd, MARKER + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    replace_file(tmp, os.path.join(wd, MARKER))
    return d


def file_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- 作業場所 → 文書
def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def project_dirs(wd, session_doc):
    """文書に入れるトラックのプロジェクト（相対の置き場）。今のトラックと、取り消しの履歴のスナップショットに
    出てくるもの（外したトラックも、Ctrl+Z で戻したときに編集ごと戻るように）。"""
    out = []
    seen = set()

    def add(t):
        d = t.get("project_dir")
        if not d or os.path.isabs(d):
            return
        d = d.replace("\\", "/")
        if d in seen:                            # 履歴に何度も出てくる置き場を、毎回見に行かない
            return
        seen.add(d)
        if os.path.exists(os.path.join(wd, d, "project.json")):
            out.append(d)
    for t in session_doc.get("tracks") or []:
        add(t)
    for e in session_doc.get("history") or []:
        if e.get("kind") != "session":
            continue
        for side in ("before", "after"):
            for t in (e.get(side) or {}).get("tracks") or []:
                add(t)
    return out


def collect(wd):
    """作業場所の中身 → 文書（dict）。"""
    sp = os.path.join(wd, SESSION_FILE)
    if not os.path.exists(sp):
        raise DocumentError("作業場所に session.json が無い: %s" % wd)
    sess = _load_json(sp)
    projects = {}
    for d in project_dirs(wd, sess):
        pj = _load_json(os.path.join(wd, d, "project.json"))
        for k in ("dir", "updated_at", "analysis"):
            pj.pop(k, None)
        projects[d] = pj
    return {"format": FORMAT, "version": VERSION, "session": sess, "projects": projects}


def content_hash(doc):
    """未保存の判定に使う中身のハッシュ（開くたびに変わるものを除く）。"""
    d = copy.deepcopy(doc)
    d.pop("saved_at", None)
    d.pop("app", None)
    d.pop("name", None)
    s = d.get("session") or {}
    for k in _VOLATILE_SESSION:
        s.pop(k, None)
    projects = d.get("projects") or {}
    for key, pj in list(projects.items()):
        for k in _VOLATILE_PROJECT:
            pj.pop(k, None)
        (pj.get("lyrics") or {}).pop("guide", None)      # ガイドの歌詞はトラックに控えてある（ガイドの写し）
        if not pj.get("edits") and not pj.get("changesets") and not any((pj.get("lyrics") or {}).values()):
            # 編集の無いトラックのプロジェクトは、トラックを選んだときに作られるだけ（ユーザーの変更ではない）
            projects.pop(key)
    _strip_rel(d)
    raw = json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _dumps(o):
    return json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# 未保存の判定（`Document.current_hash`）を、変わったファイルだけ読み直して出す（issue #63）。トラックを選ぶ
# たびに session.json（編集対象）を書くので、作業場所の全部の JSON を読み直してハッシュを取り直していた
# （実セッションで 0.14 秒）。ファイルごとに、`content_hash` に入る形（揮発する値を除いた JSON の文字列）を覚えておく
_part_cache = {}
_part_lock = threading.Lock()


def _part(path, make):
    key = os.path.normcase(os.path.abspath(path))
    st = os.stat(path)
    sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    with _part_lock:
        hit = _part_cache.get(key)
    if hit is not None and hit[0] == sig and hit[1] is make:
        return hit[2]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    value = make(json.loads(text), '"rel"' in text)
    with _part_lock:
        _part_cache[key] = (sig, make, value)
    return value


def _session_part(sess, has_rel=True):
    """session.json → (project_dirs 用の最小限, トラックの数, `content_hash` に入る文字列)。"""
    keep = {"tracks": [{"project_dir": t.get("project_dir")} for t in sess.get("tracks") or []],
            "history": [{"kind": "session",
                         "before": {"tracks": [{"project_dir": t.get("project_dir")}
                                               for t in (e.get("before") or {}).get("tracks") or []]},
                         "after": {"tracks": [{"project_dir": t.get("project_dir")}
                                              for t in (e.get("after") or {}).get("tracks") or []]}}
                        for e in sess.get("history") or [] if e.get("kind") == "session"]}
    s = dict(sess)
    for k in _VOLATILE_SESSION:
        s.pop(k, None)
    if has_rel:                                  # 「rel」がどこにも無ければ、全体をたどらない
        _strip_rel(s)
    return keep, len(sess.get("tracks") or []), _dumps(s)


def _project_part(pj, has_rel=True):
    """project.json → `content_hash` に入る文字列（編集の無いプロジェクトは None）。"""
    for k in ("dir", "updated_at", "analysis") + _VOLATILE_PROJECT:
        pj.pop(k, None)
    (pj.get("lyrics") or {}).pop("guide", None)
    if not pj.get("edits") and not pj.get("changesets") and not any((pj.get("lyrics") or {}).values()):
        return None
    if has_rel:
        _strip_rel(pj)
    return _dumps(pj)


def work_content_hash(wd):
    """`content_hash(collect(wd))` と同じ値。変わっていないファイルは読み直さない。"""
    sp = os.path.join(wd, SESSION_FILE)
    if not os.path.exists(sp):
        raise DocumentError("作業場所に session.json が無い: %s" % wd)
    keep, _n, s_raw = _part(sp, _session_part)
    parts = []
    for d in project_dirs(wd, keep):
        raw = _part(os.path.join(wd, d, "project.json"), _project_part)
        if raw is not None:
            parts.append((d, raw))
    parts.sort()
    raw = '{"format":%s,"projects":{%s},"session":%s,"version":%s}' % (
        _dumps(FORMAT), ",".join("%s:%s" % (_dumps(d), r) for d, r in parts), s_raw, _dumps(VERSION))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _walk_paths(o, fn):
    """dict の `path`（音声のパス）ごとに fn(dict) を呼ぶ。"""
    if isinstance(o, dict):
        p = o.get("path")
        if isinstance(p, str) and os.path.splitext(p)[1].lower() in AUDIO_EXTS:
            fn(o)
        for v in o.values():
            _walk_paths(v, fn)
    elif isinstance(o, list):
        for v in o:
            _walk_paths(v, fn)


def _strip_rel(doc):
    _walk_paths(doc, lambda o: o.pop("rel", None))


def _add_rel(doc, base_dir):
    def f(o):
        try:
            o["rel"] = os.path.relpath(o["path"], base_dir).replace("\\", "/")
        except ValueError:                       # 別のドライブ: 相対にできない
            o.pop("rel", None)
    _walk_paths(doc, f)


def _resolve(doc, base_dir):
    """音声のパスを今の場所に直す（絶対 → 相対 → 同じフォルダ・Media/ の同じ名前）。見つからないパスの一覧を返す。"""
    missing = []
    found = {}

    def f(o):
        p = o["path"]
        key = (p, o.get("rel"))
        if key in found:
            o["path"] = found[key]
        else:
            cands = [p]
            if o.get("rel"):
                cands.append(os.path.normpath(os.path.join(base_dir, o["rel"])))
            name = os.path.basename(p.replace("\\", "/"))
            cands += [os.path.join(base_dir, name), os.path.join(base_dir, "Media", name)]
            hit = next((c for c in cands if os.path.exists(c)), None)
            if hit is None:
                if p not in missing:
                    missing.append(p)
                hit = p
            found[key] = os.path.abspath(hit)
            o["path"] = found[key]
        o.pop("rel", None)
    _walk_paths(doc, f)
    return missing


# ---------------------------------------------------------------- 書く・読む
def write_file(doc, path, name=None, app=None):
    """文書を `.gliss` に書く（一時ファイル → 置き換え）。書いたファイルのハッシュを返す。"""
    path = os.path.abspath(path)
    d = copy.deepcopy(doc)
    d["format"], d["version"] = FORMAT, VERSION
    d["name"] = name or os.path.splitext(os.path.basename(path))[0]
    d["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if app:
        d["app"] = app
    _add_rel(d, os.path.dirname(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    replace_file(tmp, path)
    return file_hash(path)


def read_file(path):
    try:
        d = _load_json(path)
    except Exception as e:                       # noqa: BLE001
        raise DocumentError("プロジェクトのファイルを読めない: %s（%s）" % (path, e))
    if d.get("format") != FORMAT:
        raise DocumentError("Gliss のプロジェクトのファイルではない: %s" % path)
    if int(d.get("version") or 1) > VERSION:
        raise DocumentError("このファイルの版 %s はこのエンジン（%d まで）では読めない（新しい版で作られた）"
                            % (d.get("version"), VERSION))
    return d


def unpack(doc, wd, base_dir):
    """文書 → 作業場所（session.json と各トラックの project.json）。解析のキャッシュは消さない。
    見つからない音声のパスの一覧を返す。"""
    doc = copy.deepcopy(doc)
    missing = _resolve(doc, base_dir)
    os.makedirs(wd, exist_ok=True)
    sess = doc.get("session") or {}
    for d, pj in (doc.get("projects") or {}).items():
        pdir = os.path.normpath(os.path.join(wd, d))
        if not pdir.startswith(os.path.normpath(wd)):
            raise DocumentError("プロジェクトの置き場がおかしい: %s" % d)
        os.makedirs(pdir, exist_ok=True)
        pj = dict(pj, dir=pdir, analysis={})
        tmp = os.path.join(pdir, "project.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pj, f, ensure_ascii=False, indent=2)
        replace_file(tmp, os.path.join(pdir, "project.json"))
    tmp = os.path.join(wd, SESSION_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sess, f, ensure_ascii=False, indent=2)
    replace_file(tmp, os.path.join(wd, SESSION_FILE))
    return missing


def copy_caches(src, dst):
    """解析のキャッシュ（各プロジェクトの cache/）と全体の波形（overview/）を写す（名前を付けて保存で作業場所が
    変わるとき。作り直すと 158 秒の曲で数秒かかる）。再生用の音（renders/・mix/）は写さない（作り直せる・大きい）。"""
    if not os.path.isdir(src) or _norm(src) == _norm(dst):
        return
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        parts = rel.replace("\\", "/").split("/")
        if parts[0] in ("renders", "mix", "backup") or "renders" in parts:
            dirs[:] = []
            continue
        if not (("cache" in parts) or parts[0] == "overview"):
            continue
        out = os.path.join(dst, rel)
        os.makedirs(out, exist_ok=True)
        for fn in files:
            if fn.endswith(".tmp"):
                continue
            s, t = os.path.join(root, fn), os.path.join(out, fn)
            if not os.path.exists(t):
                try:
                    shutil.copy2(s, t)
                except OSError:
                    pass


def backup_work(wd):
    """作業場所の JSON（session.json・project.json）を `backup/<時刻>/` に写す（ファイルの方が外で書き換わっていて、
    作業場所にも保存していない変更があったとき。どちらも捨てない）。"""
    dst = os.path.join(wd, "backup", time.strftime("%Y%m%d-%H%M%S"))
    for root, dirs, files in os.walk(wd):
        rel = os.path.relpath(root, wd)
        if rel.split(os.sep)[0] in ("backup", "renders", "mix", "overview") or "cache" in rel.split(os.sep):
            dirs[:] = []
            continue
        for fn in files:
            if fn in (SESSION_FILE, "project.json", MARKER):
                os.makedirs(os.path.join(dst, rel), exist_ok=True)
                shutil.copy2(os.path.join(root, fn), os.path.join(dst, rel, fn))
    return dst


# ---------------------------------------------------------------- 文書（開いているもの）
class Document:
    """開いているプロジェクト（ファイル・作業場所・種類）。未保存の判定のハッシュは中身が変わったときだけ取り直す。"""

    def __init__(self, kind, work_dir, path=None, name=None):
        if kind not in KINDS:
            raise DocumentError("種類がおかしい: %r" % kind)
        self.kind = kind
        self.work_dir = os.path.abspath(work_dir)
        self.path = os.path.abspath(path) if path else None
        self.name = name or (os.path.splitext(os.path.basename(path))[0] if path else "無題")
        self._sig = None
        self._hash = None
        self.left_behind = None

    # ---- 未保存
    def _files_sig(self):
        sig = []
        for root, dirs, files in os.walk(self.work_dir):
            rel = os.path.relpath(root, self.work_dir)
            top = rel.split(os.sep)[0]
            if top in ("backup", "renders", "mix", "overview") or "cache" in rel.split(os.sep):
                dirs[:] = []
                continue
            for fn in (SESSION_FILE, "project.json"):
                if fn in files:
                    st = os.stat(os.path.join(root, fn))
                    sig.append((rel, fn, st.st_mtime_ns, st.st_size, st.st_ino))
        return tuple(sorted(sig))

    def current_hash(self):
        sig = self._files_sig()
        if sig != self._sig or self._hash is None:
            self._hash = work_content_hash(self.work_dir)
            self._sig = sig
        return self._hash

    def track_count(self):
        try:
            return _part(os.path.join(self.work_dir, SESSION_FILE), _session_part)[1]
        except Exception:                        # noqa: BLE001
            return 0

    def dirty(self):
        if self.kind == "legacy":
            return False                         # 旧形式は編集のたびに自動で保存している
        if self.kind == "untitled":
            return self.track_count() > 0
        m = read_marker(self.work_dir) or {}
        try:
            return self.current_hash() != m.get("content_hash")
        except Exception:                        # noqa: BLE001  作業場所が読めない: 保存を促す
            return True

    def info(self):
        return {"kind": self.kind, "path": self.path, "name": self.name, "work_dir": self.work_dir,
                "dirty": self.dirty(), "tracks": self.track_count(),
                "saved_at": (read_marker(self.work_dir) or {}).get("saved_at")}


def open_file(path):
    """`.gliss` を開く: 作業場所を用意して Document を返す。(Document, 見つからない音声, recovered, backup)。

    - 作業場所がまだ無い → ファイルの中身を展開する
    - 作業場所があり、ファイルが前に読んだ／保存したときのまま → **作業場所をそのまま使う**（保存していない
      変更が残っていれば、それが前回の続き = クラッシュからの復帰。recovered=True）
    - ファイルが外で書き換わっていた → 作業場所に保存していない変更があれば backup/ に写してから展開し直す
    """
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise DocumentError("プロジェクトのファイルが見つからない: %s" % path)
    doc_file = read_file(path)
    fh = file_hash(path)
    wd = work_dir_for(path)
    claim_work(wd)                               # 後回しにした削除の予定があれば取り消す（使い始める）
    m = read_marker(wd) if os.path.isdir(wd) else None
    d = Document("gliss", wd, path=path, name=doc_file.get("name"))
    missing, recovered, backup = [], False, None
    have = m is not None and os.path.exists(os.path.join(wd, SESSION_FILE))
    if have and m.get("file_hash") == fh and _norm(m.get("source") or "") == _norm(path):
        recovered = d.dirty()
        missing = _missing_now(wd)
    else:
        if have:
            try:
                dirty = content_hash(collect(wd)) != m.get("content_hash")
            except Exception:                    # noqa: BLE001
                dirty = True
            if dirty:
                backup = backup_work(wd)
                log.get().warning("作業場所に保存していない変更があったので %s に写した（ファイルが外で書き換わっていた）",
                                  backup)
            reset_work(wd)                       # 前の中身の解析のキャッシュを使わない
        missing = unpack(doc_file, wd, os.path.dirname(path))
        write_marker(wd, kind="gliss", source=path, file_hash=fh,
                     content_hash=content_hash(collect(wd)), saved_at=doc_file.get("saved_at"),
                     name=d.name)
    d.name = doc_file.get("name") or d.name
    return d, missing, recovered, backup


def _missing_now(wd):
    out = []
    try:
        s = _load_json(os.path.join(wd, SESSION_FILE))
    except Exception:                            # noqa: BLE001
        return out
    for t in s.get("tracks") or []:
        if t.get("path") and not os.path.exists(t["path"]) and t["path"] not in out:
            out.append(t["path"])
    return out


def save_file(document, path=None, app=None):
    """開いている文書をファイルに書く。path を渡せば名前を付けて保存。(書いたパス, 作業場所が変わったか)。

    作業場所は保存したファイルのもの（`work_dir_for(path)`）にそろえる（旧形式・無題・別名で保存したとき）。
    解析のキャッシュは写す。無題の作業場所は消す。旧形式のディレクトリには `moved_to.json` を置く。"""
    if path is None:
        if document.kind != "gliss" or not document.path:
            raise DocumentError("保存先が決まっていない（名前を付けて保存: path を渡す）")
        path = document.path
    path = os.path.abspath(path)
    if not path.lower().endswith(EXT):
        path += EXT
    doc = collect(document.work_dir)
    name = os.path.splitext(os.path.basename(path))[0]
    fh = write_file(doc, path, name=name, app=app)
    wd = work_dir_for(path)
    moved = _norm(wd) != _norm(document.work_dir)
    old_wd, old_kind = document.work_dir, document.kind
    if moved:
        claim_work(wd)                           # 移る先に後回しの削除の予定があれば取り消す
        if os.path.isdir(wd):
            # 同じパスに前に保存した別のプロジェクトの作業場所: 保存していない変更があれば退避して、空にしてから
            m = read_marker(wd) or {}
            if m.get("content_hash"):
                try:
                    if content_hash(collect(wd)) != m.get("content_hash"):
                        backup_work(wd)
                except Exception:                # noqa: BLE001
                    pass
            reset_work(wd)
        unpack(read_file(path), wd, os.path.dirname(path))
        copy_caches(old_wd, wd)
    saved_at = read_file(path).get("saved_at")
    write_marker(wd, kind="gliss", source=path, file_hash=fh,
                 content_hash=content_hash(collect(wd)), saved_at=saved_at, name=name)
    if moved and old_kind == "legacy":
        try:
            tmp = os.path.join(old_wd, MOVED + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"gliss": path, "saved_at": saved_at}, f, ensure_ascii=False, indent=2)
            replace_file(tmp, os.path.join(old_wd, MOVED))
        except OSError as e:
            log.get().warning("旧形式のディレクトリに %s を書けない: %s", MOVED, e)
    document.kind, document.path, document.work_dir, document.name = "gliss", path, wd, name
    document._sig = document._hash = None
    # 無題の作業場所は、呼び出し側が新しい作業場所を開いてから消す（remove_work。ログが開いているため）
    document.left_behind = old_wd if (moved and old_kind == "untitled") else None
    return path, moved


def discard(document):
    """「保存しない」: 作業場所を最後に保存した中身に戻す（ファイル）・消す（無題）。旧形式は何もしない。"""
    if document.kind == "gliss" and document.path and os.path.exists(document.path):
        doc = read_file(document.path)
        unpack(doc, document.work_dir, os.path.dirname(document.path))
        write_marker(document.work_dir, kind="gliss", source=document.path,
                     file_hash=file_hash(document.path),
                     content_hash=content_hash(collect(document.work_dir)),
                     saved_at=doc.get("saved_at"), name=document.name)
        return True
    if document.kind == "untitled":
        return remove_work(document.work_dir)
    return False


def new_untitled(name=None):
    wd = new_untitled_dir()
    os.makedirs(wd, exist_ok=True)
    s = Session(wd)
    s.history = []
    s.save()
    write_marker(wd, kind="untitled", name=name or "無題")
    return Document("untitled", wd, name=name or "無題"), s


def moved_to(legacy_dir):
    """旧形式のディレクトリを名前を付けて保存した先（`.gliss` があれば）。"""
    try:
        with open(os.path.join(legacy_dir, MOVED), encoding="utf-8") as f:
            p = json.load(f).get("gliss")
    except Exception:                            # noqa: BLE001
        return None
    return p if p and os.path.exists(p) else None


def classify(path):
    """開く対象の種類: ("gliss", path) / ("work", dir)（無題の作業場所）/ ("legacy", dir) / ("audio", path)。"""
    p = os.path.abspath(path)
    if os.path.isfile(p):
        low = p.lower()
        if low.endswith(EXT):
            return "gliss", p
        if os.path.basename(low) in (SESSION_FILE, "project.json", MARKER):
            p = os.path.dirname(p)
        elif os.path.splitext(low)[1] in AUDIO_EXTS:
            return "audio", p
        else:
            raise DocumentError("開けないファイル: %s" % path)
    if os.path.isdir(p):
        m = read_marker(p)
        if m and m.get("kind") == "untitled":
            return "work", p
        if m and m.get("kind") == "gliss" and m.get("source") and os.path.exists(m["source"]):
            return "gliss", m["source"]
        if os.path.exists(os.path.join(p, SESSION_FILE)) or os.path.exists(os.path.join(p, "project.json")):
            return "legacy", p
    raise DocumentError("プロジェクトが見つからない: %s" % path)


__all__ = ["Document", "DocumentError", "EXT", "FORMAT", "claim_work", "classify", "collect", "content_hash",
           "discard", "moved_to", "new_untitled", "open_file", "read_file", "save_file",
           "work_dir_for", "work_root", "SessionError"]
