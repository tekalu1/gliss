# -*- coding: utf-8 -*-
"""MCP: プロジェクトのファイル（新規・開く・保存。issue #33。`project/document.py`）。

画面のファイル > 新規プロジェクト（Ctrl+N）・開く（Ctrl+O）・保存（Ctrl+S）・名前を付けて保存（Ctrl+Shift+S）と
同じことを Claude Code からもする。

| ツール | 何をするか |
|---|---|
| `new_project(name?, take_path?, guide_path?)` | 空のプロジェクト（無題）を作る。`take_path` を渡すと最初のトラックとして足して編集対象にする（そのテイクの旧形式のプロジェクトが `projects/` にあれば、そちらを開く） |
| `load_project(path?)` | **引数なしで、Gliss の画面で今開いている曲**（`bridge.json`。`bridge.py`）を開く。`path` を渡すと `.gliss`・旧形式のプロジェクト（`projects/<名前>/`・その `session.json`）・無題の作業場所・音声ファイル（= `new_project(take_path)`）を開く |
| `save_project(path?)` | 保存。`path` を渡すと名前を付けて保存（旧形式・無題は `path` が要る） |
| `project_status()` | 開いているプロジェクト（種類・ファイル・名前・**未保存か**・作業場所） |
| `close_project(discard?)` | 閉じる。`discard=True` で保存していない変更を捨てる（「保存しない」） |

`mcp_server.py` の末尾から import される（`_tool` などはそちらのものを使う）。
"""
import os

from . import log
from . import mcp_server as _srv
from . import mcp_tracks as _mt
from .project import ProjectError
from .project import document as D
from .project.session import Session, SessionError, open_session

_ok = _srv._ok
_tool = _srv._tool


def current():
    """開いている文書（無ければ None）。"""
    return _srv._state.get("document")


def set_current(doc):
    _srv._state["document"] = doc


def info():
    d = current()
    return d.info() if d is not None else None


def _app_name():
    from . import __version__
    return "Gliss（engine %s）" % __version__


def _clear():
    old = _srv._state.get("project")
    if old is not None:
        from .project.pitch import forget_cache
        forget_cache(old)
    _srv._state.update(project=None, session=None, track=None, document=None)
    _srv._invalidate_renderer()
    from . import prep
    from .project import store
    prep.reset()                                 # 閉じた曲の裏の準備はやめる（issue #63）
    store.forget_open()                          # メモリに置いた Project も捨てる


def _pick_track(s, prefer=None, prefer_path=None):
    """開いたときの編集対象: 指定（id・ファイル）→ 前回選んでいたもの → 最初のボーカル（音声があるもの）。"""
    vocals = [t for t in s.vocal_tracks() if os.path.exists(t["path"])]
    for t in vocals:
        if prefer_path and os.path.normcase(os.path.abspath(t["path"])) == \
                os.path.normcase(os.path.abspath(prefer_path)):
            return t
    for tid in (prefer, s.current):
        for t in vocals:
            if tid and t["id"] == tid:
                return t
    return vocals[0] if vocals else None


def _adopt_session(s, doc, prefer=None, prefer_path=None):
    """セッションを開いて編集対象を選ぶ（ボーカルが無ければ編集対象なし）。(Project | None, 理由)。"""
    _clear()
    set_current(doc)
    t = _pick_track(s, prefer, prefer_path)
    if t is None:
        _srv._state.update(session=s, track=None, project=None)
        return None, None
    last = s.current
    p, why = s.open_project_for(t)
    s.current = t["id"]
    if last != t["id"]:
        s.save()
    _mt.adopt(s, p, t["id"])
    set_current(doc)
    return p, why


def _result(s, p, why=None, **kw):
    doc = current()
    out = dict(document=doc.info() if doc else None, session=_mt.summary(s) if s is not None else None,
               project_dir=p.dir if p else None, take=p.take if p else None,
               guide=p.guide if p else None, edits=len(p.edits) if p else 0,
               analyzed=_mt.analyzed(s, p), guide_note=why,
               next=("analyze_take を呼ぶ" if p else "add_track でトラックを足す"))
    out.update(kw)
    return _ok(**out)


def _legacy_dir_for(take_path):
    """テイクの旧形式のプロジェクト（`projects/<名前>-<sha8>/`。issue #33 より前）があればそのディレクトリ。"""
    from .audio import sha256_file
    from .media import as_clip
    from .project.store import _default_project_dir
    try:
        c = as_clip(take_path)
        if c.is_samples:
            return None
        d = _default_project_dir(c, sha256_file(os.path.abspath(c.source)))
    except Exception:                            # noqa: BLE001
        return None
    if os.path.exists(os.path.join(d, "session.json")) or os.path.exists(os.path.join(d, "project.json")):
        return d
    return None


def open_legacy(take_path=None, guide_path=None, project_dir=None, author="ai", **kw):
    """旧形式（テイクの WAV → `projects/…`）で開く（`open_project` の中身）。名前を付けて保存済みなら `.gliss` を開く。"""
    pdir = project_dir
    if pdir is None and take_path:
        pdir = _legacy_dir_for(take_path)
    if pdir is not None and D.moved_to(pdir):
        return "gliss", _load_gliss(D.moved_to(pdir), prefer_path=take_path)
    s, p, last = open_session(take_path, guide_path, project_dir, author=author, **kw)
    if s is not None:
        prim = s.primary() or s.track(s.current)
        set_current(D.Document("legacy", s.dir, name=os.path.splitext(os.path.basename(prim["path"]))[0]))
    else:
        set_current(None)
    return "legacy", (s, p, last)


def _load_gliss(path, prefer_path=None, prefer=None, fresh=False):
    doc, missing, recovered, backup = D.open_file(path)
    extra = {}
    if recovered and fresh:
        _clear()                                 # メモリに開いている同じ作業場所のプロジェクトを手放す
        D.discard(doc)                           # 作業場所に残っていた保存していない変更を捨てて、ファイルの中身から
        recovered = False
        extra["discarded_unsaved"] = True
    elif recovered:
        extra["warnings"] = ["作業場所に保存していない変更が残っていたので、その続きから開いた（recovered）。"
                             "前に当てた編集が乗ったままなので、同じ補正を当て直すと二重になる。"
                             ".gliss に保存した中身から開き直すなら load_project(path, fresh=true)"]
    s = Session.load(doc.work_dir)
    p, why = _adopt_session(s, doc, prefer=prefer, prefer_path=prefer_path)
    log.get().info("プロジェクトを開いた: %s（作業場所 %s%s）", path, doc.work_dir,
                   "・保存していない変更の続き" if recovered else "")
    return s, p, why, dict({"missing": missing, "recovered": recovered, "backup": backup}, **extra)


# ---------------------------------------------------------------- ツール
@_tool
def new_project(name: str = None, take_path: str = None, guide_path: str = None,
                author: str = "ai") -> dict:
    """新しいプロジェクト（無題）を作る。保存するまでは作業場所（%LOCALAPPDATA%\\Gliss\\work）にだけある。

    take_path: 最初のトラックにする音声（ボーカルとして足して編集対象にする）。**そのテイクの旧形式のプロジェクト
      （issue #33 より前に開いたときの `projects/<名前>-<sha8>/`）があれば、新しく作らずにそちらを開く**（前の編集が
      そのまま出る。返り値の opened = "legacy"。名前を付けて保存済みなら、その .gliss を開く = "gliss"）。
    guide_path: ガイドにするトラック（take_path と一緒のときだけ）。
    続けて add_track でトラックを足し、save_project(path) で .gliss に保存する。
    """
    if take_path:
        if not os.path.exists(take_path):
            raise ProjectError("音声が見つからない: %s" % take_path)
        if _legacy_dir_for(take_path) is not None:
            kind, r = open_legacy(take_path, guide_path, author=author)
            if kind == "gliss":
                s, p, why, extra = r
                return _result(s, p, why, opened="gliss", **extra)
            s, p, last = r
            _mt.adopt(s, p, s.current)
            _g, why = s.guide_clip_for(s.track(s.current))
            out = _result(s, p, why, opened="legacy",
                          note="旧形式のプロジェクト（%s）を開いた。名前を付けて保存で .gliss にできる" % os.path.basename(s.dir))
            out["session"]["last_current"] = last        # 前回選んでいたトラック（画面が戻す）
            return out
    doc, s = D.new_untitled(name)
    _clear()
    set_current(doc)
    _srv._state.update(session=s, track=None, project=None)
    if not take_path:
        return _result(s, None, opened="new")
    t = s.add_track(take_path, kind="vocal")
    s.detect_tempo()
    s.save()
    if guide_path:
        g = s.find(guide_path) or s.add_track(guide_path, kind="vocal")
        s.guide = g["id"]
        s.save()
    p, why = _adopt_session(s, doc, prefer=t["id"])
    return _result(s, p, why, opened="new")


def _app_project():
    """画面（Gliss）で今開いている曲（bridge.json）。(開くパス, 編集中のトラックの id)。無ければ例外。"""
    from . import bridge
    cur = bridge.project()
    if cur is None:
        raise ProjectError("Gliss の画面で開いている曲が無い（Gliss で曲を開いてから呼ぶか、path を渡す）。"
                           "見ている場所: %s" % bridge.path())
    if not os.path.exists(cur["path"]):
        raise ProjectError("Gliss の画面で開いている曲が見つからない: %s" % cur["path"])
    return cur["path"], cur.get("track")


@_tool
def load_project(path: str = None, author: str = "ai", fresh: bool = False) -> dict:
    """プロジェクトを開く。**引数なしで呼ぶと、Gliss の画面で今開いている曲**（と画面で編集中のトラック）を開く
    （画面と同じ作業場所を使うので、編集は画面に即反映される。返り値の from_app = true）。
    画面で何も開いていなければエラー。path:
    - `<名前>.gliss`（保存したプロジェクト）。前に開いたときの作業場所があって、保存していない変更が残っていれば
      その続きから開く（返り値の recovered = true。クラッシュ・保存せずに落ちたとき）
    - 旧形式のプロジェクトのディレクトリ（`projects/<テイク名>-<sha8>/`）かその `session.json` / `project.json`。
      今までどおり編集のたびに自動で保存される（名前を付けて保存すると .gliss になる）
    - 無題の作業場所（`…\\Gliss\\work\\untitled-…`）
    - 音声ファイル（= new_project(take_path=…)）
    見つからない音声は missing に返す（ファイルと一緒に動かしたなら、.gliss からの相対パス・同じフォルダの同じ名前でも探す）。
    recovered = true のときは warnings にもその旨を入れる。fresh=True（.gliss のとき）は、作業場所に残っていた
    保存していない変更を捨てて、ファイルに保存した中身から開く（close_project(discard=true) → load_project と同じ。
    返り値の discarded_unsaved）。
    """
    track = None
    if not path:
        path, track = _app_project()
        out = _load(path, author, track, fresh=fresh)
        out["from_app"] = True
        return out
    return _load(path, author, track, fresh=fresh)


def _load(path, author, track=None, fresh=False):
    """load_project の中身。track: 開いたときの編集対象にしたいトラックの id（画面で編集中のもの）。"""
    kind, p0 = D.classify(path)
    if kind == "audio":
        return new_project(take_path=p0, author=author)
    if kind == "gliss":
        s, p, why, extra = _load_gliss(p0, prefer=track, fresh=fresh)
        return _result(s, p, why, opened="gliss", **extra)
    if kind == "work":
        D.claim_work(p0)                         # 後回しにした削除の予定があれば取り消す（使い始める）
        m = D.read_marker(p0) or {}
        doc = D.Document("untitled", p0, name=m.get("name") or "無題")
        s = Session.load(p0)
        p, why = _adopt_session(s, doc, prefer=track)
        return _result(s, p, why, opened="untitled", missing=D._missing_now(p0))
    # 旧形式
    if D.moved_to(p0):
        s, p, why, extra = _load_gliss(D.moved_to(p0), prefer=track)
        return _result(s, p, why, opened="gliss", note="名前を付けて保存したプロジェクトを開いた", **extra)
    if Session.exists(p0):
        D.claim_work(p0)
        s = Session.load(p0)
        doc = D.Document("legacy", p0, name=os.path.basename(os.path.normpath(p0)))
        p, why = _adopt_session(s, doc, prefer=track)
        return _result(s, p, why, opened="legacy", missing=D._missing_now(p0))
    import json
    with open(os.path.join(p0, "project.json"), encoding="utf-8") as f:
        take = (json.load(f).get("take") or {}).get("path")
    if not take or not os.path.exists(take):
        raise ProjectError("旧形式のプロジェクトのテイクが見つからない: %s" % take)
    _kind, (s, p, last) = open_legacy(take, None, project_dir=p0, author=author)
    _mt.adopt(s, p, s.current)
    _g, why = s.guide_clip_for(s.track(s.current))
    return _result(s, p, why, opened="legacy")


@_tool
def save_project(path: str = None) -> dict:
    """保存する。path を渡すと名前を付けて保存（拡張子 .gliss を付ける）。旧形式・無題は path が要る。

    保存すると未保存が消える（project_status の dirty = false）。名前を付けて保存したら、以後の編集はそのファイルの
    作業場所に入る（旧形式のディレクトリには moved_to.json を置く。同じテイクを開き直すとこの .gliss を開く）。
    """
    doc = current()
    if doc is None:
        raise ProjectError("プロジェクトが開かれていない（new_project / load_project）")
    s = _srv._state.get("session")
    if s is not None:
        s.reload_if_changed()
        s.save()                                 # メモリの変更を作業場所に（書き忘れを残さない）
    tid = _mt.current_track_id()
    old_wd = doc.work_dir
    out, moved = D.save_file(doc, path, app=_app_name())
    p = _srv._state.get("project")
    if moved:
        # 作業場所が変わった: 新しい作業場所のセッション・編集対象のプロジェクトを開き直す
        s = Session.load(doc.work_dir)
        p, why = _adopt_session(s, doc, prefer=tid)
        if doc.left_behind:
            D.remove_work(doc.left_behind)      # 無題の作業場所（新しい方を開いてから。ログを離す）
            doc.left_behind = None
    else:
        why = None
    log.get().info("保存した: %s（作業場所 %s%s）", out, doc.work_dir,
                   "・%s から移した" % old_wd if moved else "")
    return _result(s, p, why, saved=out, moved=moved,
                   next=("analyze_take を呼ぶ（作業場所が変わった）" if moved and p else None))


@_tool
def project_status() -> dict:
    """開いているプロジェクト: kind（"gliss" = 保存したファイル / "untitled" = 無題 / "legacy" = 旧形式の projects/）・
    path（.gliss）・name・**dirty（保存していない変更があるか。旧形式は自動で保存しているので常に false）**・
    work_dir（作業場所）・tracks（トラックの数）・saved_at。開いていなければ document = null。"""
    return _ok(document=info())


@_tool
def close_project(discard: bool = False) -> dict:
    """プロジェクトを閉じる。discard=True で保存していない変更を捨てる（.gliss は最後に保存した中身に戻し、
    無題は作業場所ごと消す。旧形式は自動で保存しているので何もしない）。discard=False なら作業場所に残る
    （同じファイルを開けば続きから）。"""
    doc = current()
    dropped = False
    _clear()
    if doc is not None and discard:
        dropped = D.discard(doc)
    return _ok(closed=({"kind": doc.kind, "path": doc.path, "name": doc.name} if doc else None),
               discarded=bool(dropped), removal=dropped if isinstance(dropped, str) else None)


TOOLS = [new_project, load_project, save_project, project_status, close_project]

__all__ = ["TOOLS", "current", "info", "open_legacy", "SessionError"]
