# -*- coding: utf-8 -*-
"""MCP: トラック（セッション）のツール（issue #7。`docs/track-view.md` §1・§2・§4・§5）。

既存のツール（`shift_pitch` など）は**編集対象のトラック**（`select_track` で選んだもの）のプロジェクトに
今までどおり効く。ここにあるのは、トラックの一覧・選択・追加・削除・位置（音源全体をずらす）・
ミュート／ソロ・ガイドの指定と、画面の描画・再生に使う全体の波形（`track_overview`）と
トラックごとの音（`render_tracks`）。

`mcp_server.py` の末尾から import される（`_tool` などはそちらのものを使う）。

**取り消しの履歴**（issue #16）もここに置く: 編集のツール（mcp_server.py）とトラックのツールが入れた
変更を、セッションの 1 本の履歴（`session.py`）に足す。`undo` / `redo`（引数なし）はこの履歴を戻す
（別のトラックの操作なら、そのトラックを編集対象にしてから戻す）。
"""
import copy
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import ExitStack, contextmanager

import numpy as np
import soundfile as sf

from . import log
from . import mcp_server as _srv
from . import prep
from .project import Project, ProjectError
from .project.store import replace_file
from .project.session import (CUT_EPS, CUT_MIN_EDGE, MUTE_MIN, SessionError, covered, norm_gain_db, norm_mutes,
                              norm_pan, pieces_of, set_guide_id, subtract_range)

_ok = _srv._ok
_tool = _srv._tool
PLAYABLE_EXTS = (".wav", ".wave", ".flac")     # 画面（Chromium の decodeAudioData）がそのまま読める
OVERVIEW_VERSION = 2
OVERVIEW_HOPS = (32, 128, 512, 2048, 8192)
KEEP_STEMS = 3


# ---------------------------------------------------------------- 共通
def _session(required=True):
    s = _srv._state.get("session")
    if s is None:
        if required:
            raise ProjectError("トラックが無い。先に open_project を呼ぶこと。")
        return None
    if s.reload_if_changed():
        _schedule(s)                             # 外部でトラック・ガイドが変わった（issue #63）
    return s


def _schedule(s):
    """裏の準備（issue #63）の順番待ちを、今のセッションに合わせる（足した・ガイドを変えた・取り消した…）。"""
    try:
        prep.schedule(s, current=current_track_id())
    except Exception as e:                       # noqa: BLE001  準備が入れられなくても操作は通す
        log.get().warning("裏の準備に入れられない: %s", e)


def reschedule_prep():
    """ピッチ検出の方式を替えた（`set_f0_estimator`）: 裏の準備を今の方式の組み合わせで入れ直す。"""
    s = _srv._state.get("session")
    if s is not None:
        _schedule(s)


EXPLICIT_KEY = "estimator_explicit"   # session.json のトラック: 方式を利用者が明示した（アーカイブ・方式探しで決まったものでない）


def _session_track_of(p, target=None):
    """プロジェクト p のトラック (セッション, トラック id)。target = (セッション, トラック) を渡せばそれ
    （ジョブの終わりなど、編集対象が別のトラックへ戻った後でも、頼まれたときのトラックへ書く）。"""
    if target is not None:
        s, t = target
        if (s is not None and t is not None and p is not None and t.get("project_dir")
                and _norm(p.dir) == _norm(s.project_dir_of(t))):
            s.reload_if_changed()
            try:
                s.track(t["id"])
            except ProjectError:
                return None, None
            return s, t["id"]
    return _session_of(p)


def remember_estimator(p, est, target=None):
    """analyze_take で明示した F0 の方式を、そのトラックの方式として覚える（選んでいる方式と同じなら外す）。
    session.json のトラックに `estimator` として保存し、裏の準備の署名・解析もそれを使う（準備が既定の方式で
    解析し直して差し替えない）。セッションのトラックでなければ（単独のプロジェクト）メモリの上だけ。
    target: 頼まれたときの (セッション, トラック)（`prep_target`）。中継が編集対象を一時的に切り替えて呼んだ
    ジョブは、終わるときには編集対象が戻っているので、編集対象からはトラックが引けない。"""
    from .analysis import f0 as f0mod
    pref = None if est == f0mod.resolve_estimator() else est
    p.estimator_pref = pref
    s, tid = _session_track_of(p, target)
    if s is None:
        return
    t = s.track(tid)
    if s.estimator_of(t) != pref or not t.get(EXPLICIT_KEY):
        if pref is None:
            t.pop("estimator", None)
        else:
            t["estimator"] = pref
        t[EXPLICIT_KEY] = True                   # 利用者が明示した: ara_render_dirty の方式探し（_fit_estimator）で戻さない
        s.save()
        reschedule_prep()


def mark_explicit(p, target=None):
    """トラックの今の方式を、利用者が明示した方式として印を付ける（`analyze_take(estimator=…)` が今の方式と同じとき）。"""
    s, tid = _session_track_of(p, target)
    if s is None:
        return
    t = s.track(tid)
    if not t.get(EXPLICIT_KEY):
        t[EXPLICIT_KEY] = True
        s.save()


def estimators_now(s, p, track_ids=None):
    """方式を替える前後で比べる、トラックごとの (Project, 実効の F0 の方式)。{トラック id: (q, 方式)}。
    セッションが無ければ開いている曲だけ（鍵 "project"）。まだ解析していない・プロジェクトの無いトラックは入れない。"""
    out = {}
    if s is None:
        if p is not None:
            out["project"] = (p, p.f0_estimator())
        return out
    for t in s.vocal_tracks():
        if track_ids is not None and t["id"] not in track_ids:
            continue
        q, _ = _track_project(s, t)
        if q is None or not (q.analysis or {}).get("take"):
            continue
        q.estimator_pref = t.get("estimator")
        try:
            out[t["id"]] = (q, q.f0_estimator())
        except Exception:                            # noqa: BLE001  使えない方式（重みが無い）: 比べない
            continue
    return out


def retarget_switched(s, p, before, track_ids=None):
    """方式を替えた後: 実効の方式が替わったトラックの、ノートの ID を対象にした編集を、替える前の解析での区間の
    範囲対象に付け替える（`Project.retarget_note_targets`。音は変わらない。次の解析で同じ番号の別のノートに当たらない）。
    before = 替える前の `estimators_now`。{トラック id: 付け替えの結果}（付け替える編集の無いトラックは入れない）。"""
    after = estimators_now(s, p, track_ids)
    res = {}
    for key, (_q, old) in before.items():
        if key not in after:
            continue
        q, new = after[key]
        if new == old:
            continue
        r = q.retarget_note_targets(basis=old)
        if r["retargeted"] or r["history"] or r["unresolved"] or r["skipped"]:
            res[key] = r
    return res


def forget_track_estimators():
    """`set_f0_estimator` で選び直した: トラックごとに明示した方式をすべて外す。外したものがあれば True。"""
    p = _srv._state.get("project")
    if p is not None:
        p.estimator_pref = None
    s = _srv._state.get("session")
    if s is None:
        return False
    s.reload_if_changed()
    had = [t for t in s.tracks if s.estimator_of(t) or t.get(EXPLICIT_KEY)]
    for t in had:
        t.pop("estimator", None)
        t.pop(EXPLICIT_KEY, None)
    if had:
        s.save()
    return bool(had)


def estimator_snapshot(s, track_ids=None):
    """方式変更前後の明示方式と、各トラックに必要な解析方式を保存する。"""
    from .analysis import f0 as F

    rows = {}
    for t in s.vocal_tracks():
        if track_ids is not None and t["id"] not in track_ids:
            continue
        q, _ = _track_project(s, t)
        if q is None:
            rows[t["id"]] = {"pref": t.get("estimator"), "explicit": bool(t.get(EXPLICIT_KEY)),
                             "effective": None, "analyzed": False}
            continue
        q.estimator_pref = t.get("estimator")
        effective = q.f0_estimator()
        take = (q.analysis or {}).get("take") or {}
        model_version = (q.f0_model_version or
                         (take.get("estimator_version") if take.get("estimator") == "gliss" else None) or
                         F.estimator_version("gliss")) if effective == "gliss" else None
        rows[t["id"]] = {"pref": t.get("estimator"), "explicit": bool(t.get(EXPLICIT_KEY)), "effective": effective,
                         "analyzed": bool(take), "model_version": model_version}
    return {"chosen": F.chosen_estimator(), "tracks": rows}


@contextmanager
def _recover_projects(paths):
    """解析・アーカイブ復元が失敗したら、履歴が指す前のディスク状態に戻す。"""
    with tempfile.TemporaryDirectory(prefix="gliss-history-") as temp:
        saved = []
        for i, path in enumerate(dict.fromkeys(paths)):
            if os.path.isdir(path):
                backup = os.path.join(temp, str(i))
                shutil.copytree(path, backup, symlinks=True)
                saved.append((path, backup))
        try:
            yield
        except BaseException:
            for path, backup in saved:
                shutil.copytree(backup, path, dirs_exist_ok=True, symlinks=True)
            raise


@contextmanager
def _estimator_exclusive(s, track_ids=None):
    """方式の履歴とプロジェクトを保存し終えるまで、対象の裏準備を止める。"""
    paths = [s.project_dir_of(t) for t in s.vocal_tracks()
             if track_ids is None or t["id"] in track_ids]
    with ExitStack() as guard:
        for path in dict.fromkeys(paths):
            guard.enter_context(prep.exclusive(path))
        yield


def _restore_estimator_history(s, state):
    """方式名を戻すだけで終わらせず、保存された方式で解析を復元する。"""
    from .analysis import f0 as F

    rows = state["tracks"]
    for tid, row in rows.items():
        est = row.get("effective")
        if est == "rmvpe" and not F.rmvpe_available():
            raise ProjectError("ピッチ検出の方式を復元できない: rmvpe の重みが無い")
    F.set_preferred_estimator(state.get("chosen"))
    for tid, row in rows.items():
        try:
            t = s.track(tid)
        except ProjectError:
            continue
        q, _ = _track_project(s, t)
        if q is not None and (q.analysis or {}).get("take") and row.get("effective"):
            # 方式が替わる: 今の解析のうちにノート対象の編集を区間の範囲対象へ付け替える（音は変わらない）
            q.estimator_pref = t.get("estimator")
            try:
                if q.f0_estimator() != row["effective"]:
                    q.retarget_note_targets()
            except Exception as e:                   # noqa: BLE001  使えない方式: 付け替えずに戻す
                log.get().warning("方式の履歴を戻す前の付け替えを飛ばした: %s", e)
        pref = row.get("pref")
        if pref is None:
            t.pop("estimator", None)
        else:
            t["estimator"] = pref
        if row.get("explicit"):
            t[EXPLICIT_KEY] = True
        else:
            t.pop(EXPLICIT_KEY, None)
        q, is_cur = _track_project(s, t)
        if q is None:
            continue
        q.estimator_pref = pref
        est = row.get("effective")
        old_version = q.f0_model_version
        if est == "gliss":
            # 旧履歴には版が無い。その履歴が作られたときの同梱モデルは v2。
            q.f0_model_version = row.get("model_version", F.GLISS_F0_V2_VERSION)
        else:
            q.f0_model_version = None
        if est and (row.get("analyzed") or (q.analysis or {}).get("take")) and not q.analysis_cached(est):
            q.analyze(estimator=est)
        elif q.f0_model_version != old_version:
            q.save()
        if is_cur:
            _srv._invalidate_renderer()


def _commit_estimator_history(s, e, state, undone):
    """解析・方式・履歴印を同時に確定し、保存失敗時はすべて戻す。"""
    from .analysis import f0 as F

    rows = state["tracks"]
    paths = [s.project_dir_of(t) for t in s.tracks if t["id"] in rows]
    with _estimator_exclusive(s, set(rows)):
        old_chosen = F.chosen_estimator()
        old_tracks = copy.deepcopy(s.tracks)
        old_history = copy.deepcopy(s.history)
        old_marks = dict(s.history_marks)
        with open(s.path, "rb") as f:
            old_session = f.read()
        cur = _srv._state.get("project")
        try:
            with _recover_projects(paths):
                _restore_estimator_history(s, state)
                e["undone"] = undone
                s.save()
                _schedule(s)
        except BaseException:
            F.set_preferred_estimator(old_chosen)
            s.tracks, s.history, s.history_marks = old_tracks, old_history, old_marks
            with open(s.path, "rb") as f:
                changed = f.read() != old_session
            if changed:
                with tempfile.NamedTemporaryFile(dir=s.dir, prefix="session-rollback-",
                                                 suffix=".tmp", delete=False) as f:
                    f.write(old_session)
                    tmp = f.name
                replace_file(tmp, s.path)
            s._sig = s._stat()
            if cur is not None:
                cur.load()
                cur._forget_analysis()
            _srv._invalidate_renderer()
            raise


def prep_target(p):
    """analyze_take の合流先: p が編集対象のトラックのもので、ガイドが今の指定どおりなら (セッション, トラック)。"""
    s, tid = _session_of(p)
    if s is None:
        return None
    try:
        t = s.track(tid)
        if t["kind"] != "vocal" or s.guide_stale(t, p):
            return None
    except ProjectError:
        return None
    return s, t


def _guarded(fn):
    """セッションを書き換えるツール: 途中で失敗したら、メモリのセッションを session.json の内容に戻す
    （検証に落ちた変更が、次の保存でそのまま書かれないように）。"""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:
            s = _srv._state.get("session")
            if s is not None and os.path.exists(s.path):
                try:
                    s._read()
                except Exception:                # noqa: BLE001
                    pass
            raise
    return wrapper


def current_track_id():
    return _srv._state.get("track")


def current_offset_sec():
    """編集対象のトラックのタイムライン上の位置（秒）。セッションが無ければ 0。"""
    s = _srv._state.get("session")
    tid = current_track_id()
    if s is None or not tid:
        return 0.0
    try:
        s.reload_if_changed()
        t = s.track(tid)
        # プロジェクトを直接差し替えた（セッションを通さずに開いた）ときは、そのトラックの位置を使わない
        p = _srv._state.get("project")
        if p is None or os.path.normcase(p.dir) != os.path.normcase(s.project_dir_of(t)):
            return 0.0
        return float(t["offset_sec"])
    except ProjectError:
        return 0.0


def current_mutes():
    """編集対象のトラックの、クリップで消している区間（トラックの頭が 0 の秒。書き出しで 0 にする）。"""
    s = _srv._state.get("session")
    tid = current_track_id()
    if s is None or not tid:
        return []
    try:
        s.reload_if_changed()
        t = s.track(tid)
        p = _srv._state.get("project")
        if p is None or os.path.normcase(p.dir) != os.path.normcase(s.project_dir_of(t)):
            return []
        return [list(m) for m in t.get("mutes") or []]
    except ProjectError:
        return []


def summary(s=None, extra=None):
    s = s if s is not None else _srv._state.get("session")
    if s is None:
        return None
    s.ensure_history()
    p = _srv._state.get("project")
    out = s.summary(current=current_track_id(), project=p)
    tid = current_track_id()
    if p is not None and tid:
        try:
            out["guide_stale"] = s.guide_stale(s.track(tid), p)
        except ProjectError:
            out["guide_stale"] = False
    if tid:
        # 編集対象にガイドが重ならない理由（ガイドが無い・ガイドのトラック自身・位置が重ならない…）。
        # 画面はこれをステータス行と「ガイドに合わせる」の使えない理由に出す（issue #32）
        try:
            t = s.track(tid)
            out["guide_note"] = (s.guide_clip_for(t)[1] if t.get("guide_id") or s.guide
                                 else "ガイドが指定されていない" if t["kind"] == "vocal" else None)
        except ProjectError:
            out["guide_note"] = None
    # 裏の準備の状態（issue #63）: ready / preparing（stage・progress）/ queued / failed。画面の見出しの印
    for d in out["tracks"]:
        d["prep"] = prep.status(s.dir, d["id"]) if d["kind"] == "vocal" else None
    # 開いているプロジェクトのファイル（未保存か。画面はタイトルの「*」に使う。issue #33）
    from . import mcp_document
    try:
        out["document"] = mcp_document.info()
    except Exception as e:                       # noqa: BLE001  要約が作れなくてもトラックは返す
        log.get().warning("プロジェクトの状態を作れない: %s", e)
        out["document"] = None
    if extra:
        out.update(extra)
    return out


def adopt(s, p, track_id):
    """open_project の後: セッションと編集対象を覚える（mcp_server.open_project から呼ぶ）。"""
    old = _srv._state.get("project")
    if old is not None and old is not p:
        from .project.pitch import forget_cache
        forget_cache(old)                        # 閉じたプロジェクトの自動の幅のキャッシュを捨てる
        # 閉じたプロジェクトはメモリに置いておく（issue #63）が、音声は共有の音声キャッシュに任せて持たない
        old._audio_cache = {}
    _srv._state["session"] = s
    _srv._state["track"] = track_id
    _srv._state["project"] = p
    _srv._invalidate_renderer()
    if s is not None:
        _schedule(s)                             # 開いた・選んだ: 今のトラックを先頭に（issue #63）
    else:
        prep.reset()


def _open_track(s, t):
    p, why = s.open_project_for(t)
    s.current = t["id"]
    s.save()
    adopt(s, p, t["id"])
    return p, why


def analyzed(s, p):
    """編集対象の解析が済んでいて、analyze_take がキャッシュを読むだけで済むか（画面の「解析済み。読み込んでいる…」）。

    裏の準備の済みの印（今のガイドとの組み合わせまで）で見る。印が無い前の版のプロジェクトは、
    テイクの解析と（ガイドがあれば）対応付けの記録があるかで見る。"""
    if p is None:
        return False
    t = prep_target(p) if s is not None else None
    if t is not None:
        if prep.is_ready(*t):
            return True
        sig = prep.track_sig(*t)
        pdir = t[0].project_dir_of(t[1])
        if prep.has_stamp(pdir, sig):
            return False
    try:
        if p.analysis_cached():                  # 鍵付きの保存がそろっている（一度組んだガイド。issue #63）
            return True
    except Exception:                            # noqa: BLE001  分からなければ下の目安で
        pass
    if not p.analysis.get("take"):
        return False
    return p.guide is None or bool(p.analysis.get("alignment"))


def _project_result(s, p, why=None, **kw):
    return _ok(project_dir=p.dir, log=p.log_path, take=p.take, guide=p.guide,
               edits=len(p.edits), changesets=len(p.changesets),
               analyzed=analyzed(s, p),
               guide_note=why,
               session=summary(s),
               next="analyze_take を呼ぶ（ガイドの解析・対応付けもここで走る）", **kw)


def _reopen_if_stale(s):
    """ガイドの指定・位置が変わって、編集対象のプロジェクトのガイドが古くなったら開き直す。"""
    tid = current_track_id()
    p = _srv._state.get("project")
    if not tid or p is None:
        return False
    try:
        t = s.track(tid)
    except SessionError:
        return False
    if not s.guide_stale(t, p):
        return False
    _open_track(s, t)
    return True


# ---------------------------------------------------------------- 一覧・選択
@_tool
def list_tracks() -> dict:
    """セッションのトラックの一覧（名前・種類・位置・長さ・ミュート／ソロ・ガイド・編集対象）。

    kind: "vocal"（ボーカルのテイク。編集できる。1 本をガイドに指定できる）/ "inst"（伴奏。聴くだけ）。
    offset_sec: **タイムライン上の位置**（音源全体をずらした量。既定 0 = 曲頭 0:00 起点）。
    編集の秒（list_notes などの秒）は、そのトラックの頭が 0（タイムラインの秒 = offset_sec + 編集の秒）。
    guide / guide_id / effective_guide_id / is_guide: guide = 共通のガイド（set_guide_track）、guide_id = そのトラックに明示した
      ガイド（set_track_guide。null なら共通のガイド）、effective_guide_id = 実際に使うガイド、is_guide = どれかのトラックの
      実効のガイドになっている（guide_for = それを使うトラックの id）。
    guide_stale: 編集対象のプロジェクトのガイドが古い（外部でガイド・位置を変えた。select_track で開き直す）。
    cuts: クリップを分けた切れ目（トラックの頭が 0 の秒。split_track / join_track）。
    mutes: クリップで消している区間 [[始め, 終わり]…]（同じ秒。mute_track_range。再生・書き出しで鳴らさない）。
    """
    s = _session()
    return _ok(**summary(s))


@_tool
@_guarded
def select_track(track_id: str) -> dict:
    """編集対象のトラックを切り替える。以後の編集ツール（shift_pitch など）はこのトラックに効く。

    ガイドはセッションの指定（set_guide_track）から、タイムライン上の位置を合わせて重ねる。
    切り替えたら analyze_take を呼ぶ（解析済みならキャッシュで速い）。伴奏のトラックは選べない。
    """
    s = _session()
    t = s.track(track_id)
    p, why = _open_track(s, t)
    return _project_result(s, p, why)


# ---------------------------------------------------------------- 足す・消す・変える
@_tool
@_guarded
def add_track(path: str, kind: str = None, name: str = None, offset_sec: float = 0.0,
              guide: bool = False, select: bool = False, author: str = "ai") -> dict:
    """音声ファイル（WAV など）をトラックとして足す。取り消せる（undo）。

    kind: "vocal" / "inst"。省略するとファイル名から推す（inst・karaoke・オケ・伴奏 などは伴奏）
    offset_sec: タイムライン上の位置（既定 0 = 曲頭 0:00 起点）
    guide: True ならこのトラックをガイドに指定する（もうトラックにあるファイルなら指定だけする）
    select: True なら編集対象にする（伴奏になったトラックは選ばない。返り値の selected）
    """
    s = _session()
    before, cur0 = s.snapshot(), current_track_id()
    if kind is not None and kind not in ("vocal", "inst"):
        raise SessionError("kind は vocal か inst")
    t = s.find(path)
    existed = t is not None
    if t is None:
        t = s.add_track(path, kind=kind, name=name, offset_sec=offset_sec or 0.0)
    elif not guide and not select and kind in (None, t["kind"]):
        raise SessionError("もうトラックにある: %s（%s）" % (os.path.basename(path), t["id"]))
    elif kind is not None and kind != t["kind"]:
        if kind == "inst" and t["id"] == current_track_id():
            raise SessionError("編集中のトラックは伴奏にできない")
        s.set_kind(t, kind)
    if guide:
        if t["kind"] != "vocal":
            raise SessionError("伴奏のトラックはガイドにできない（kind を vocal にする）")
        s.guide = t["id"]
    label = "トラックの追加" if not existed else ("ガイドの指定" if guide else "伴奏／ボーカルの扱い")
    if not existed:
        s.detect_tempo()        # テンポがまだ無ければ iXML から（取り消すとトラックと一緒に消える。issue #18）
    _record_session(s, label, t["id"], before, cur0,
                    t["id"] if select and t["kind"] == "vocal" else cur0, author)
    s.save()
    if select and t["kind"] == "vocal":
        p, why = _open_track(s, t)
        return _project_result(s, p, why, track=t["id"], existed=existed, selected=True)
    reopened = _reopen_if_stale(s)
    _schedule(s)                                 # 足したトラックの準備を裏で始める（issue #63）
    return _ok(track=t["id"], kind=t["kind"], existed=existed, selected=False, reopened=reopened,
               session=summary(s),
               next=("analyze_take を呼ぶ（ガイドが変わった）" if reopened else "list_tracks"))


@_tool
@_guarded
def remove_track(track_id: str, author: str = "ai") -> dict:
    """トラックをセッションから外す（音声ファイルと、そのトラックの編集＝プロジェクトのディレクトリは消さない）。
    取り消せる（undo で同じ id・同じ位置に戻る。編集もそのまま）。

    編集対象のトラックを外したら、残っている最初のボーカルのトラックを編集対象にする。
    ボーカルのトラックが 1 本も無くなる外し方はできない。
    """
    s = _session()
    before, cur0 = s.snapshot(), current_track_id()
    t = s.track(track_id)
    if t["kind"] == "vocal" and len(s.vocal_tracks()) <= 1:
        raise SessionError("ボーカルのトラックが無くなるので外せない")
    s.remove_track(track_id)
    nxt0 = s.vocal_tracks()[0]["id"] if cur0 == track_id else cur0
    _record_session(s, "トラックを外す", track_id, before, cur0, nxt0, author)
    s.save()
    switched = None
    if current_track_id() == track_id:
        nxt = s.vocal_tracks()[0]
        _open_track(s, nxt)
        switched = nxt["id"]
    reopened = switched is not None or _reopen_if_stale(s)
    _schedule(s)
    return _ok(removed=track_id, switched_to=switched, reopened=reopened, session=summary(s),
               next=("analyze_take を呼ぶ" if reopened else "list_tracks"))


@_tool
@_guarded
def set_track(track_id: str, name: str = None, kind: str = None, mute: bool = None,
              solo: bool = None, offset_sec: float = None, index: int = None,
              gain_db: float = None, pan: float = None, author: str = "ai") -> dict:
    """トラックの名前・種類・ミュート／ソロ・**音量・パン**・**位置**・**並び順**を変える（渡したものだけ）。
    単体版では名前・種類・位置・並び順・ミュート／ソロ・音量・パンを取り消せる（undo）。
    ARA のミキサーは DAW が所有するため、Gliss の履歴には入れない。

    gain_db: トラックの音量（dB。既定 0。−60〜+6 に丸め、−60 以下は無音 = −∞）。**再生（画面）だけに効く**
      （書き出し・render_tracks の音のファイルには入らない）。session（.gliss）には保存する
    pan: トラックのパン（−1 = 左いっぱい 〜 +1 = 右いっぱい、既定 0）。再生だけに効く

    offset_sec: タイムライン上の位置（秒。負も可）。**音源全体を非破壊でずらす**（ファイルは書き換えない）。
      編集は音と一緒に動く（編集の秒はトラックの頭が 0 のまま）。ガイドとの対応はタイムライン上の位置で
      取り直すので、編集対象かガイドのトラックをずらしたら analyze_take を呼ぶ（返り値の reopened）。
      書き出し（export_wav）は元と同じ長さのまま、BWF の TimeReference（DAW 上の位置）をずらした量だけ動かす。
    kind: "vocal" / "inst"（編集対象のトラックは伴奏にできない。ガイドを伴奏にしたらガイドの指定が外れる）
    index: トラックの並びの何番目に置くか（0 が一番上。範囲の外は端に丸める。issue #38）。
      並びは見た目だけ（音・編集・ガイドとの対応は変わらない）
    """
    s = _session()
    before, cur0 = s.snapshot(), current_track_id()
    t = s.track(track_id)
    old = dict(t)
    moved = False
    if index is not None:
        i = int(index)
        rest = [x for x in s.tracks if x["id"] != track_id]
        i = max(0, min(len(rest), i))
        order = rest[:i] + [t] + rest[i:]
        moved = [x["id"] for x in order] != [x["id"] for x in s.tracks]
        s.tracks = order
    if offset_sec is not None and not np.isfinite(float(offset_sec)):
        raise SessionError("offset_sec が数ではない")
    for key, val in (("gain_db", gain_db), ("pan", pan)):
        if val is not None and not np.isfinite(float(val)):
            raise SessionError("%s が数ではない" % key)
    if kind is not None:
        if kind not in ("vocal", "inst"):
            raise SessionError("kind は vocal か inst")
        if kind == "inst" and track_id == current_track_id():
            raise SessionError("編集中のトラックは伴奏にできない（先に別のトラックを選ぶ）")
        if kind == "inst" and t["kind"] == "vocal" and len(s.vocal_tracks()) <= 1:
            raise SessionError("ボーカルのトラックが無くなるので伴奏にできない")
        s.set_kind(t, kind)                      # 伴奏にしたら、このトラックの指定と、これを指していた指定を外す
    if name is not None:
        t["name"] = str(name).strip() or t["name"]
    if mute is not None:
        t["mute"] = bool(mute)
    if solo is not None:
        t["solo"] = bool(solo)
    if gain_db is not None:
        t["gain_db"] = norm_gain_db(gain_db)
    if pan is not None:
        t["pan"] = norm_pan(pan)
    if offset_sec is not None:
        v = float(offset_sec)
        if not np.isfinite(v):
            raise SessionError("offset_sec が数ではない")
        t["offset_sec"] = round(v, 6)
    what = [lab for k, lab in (("offset_sec", "トラックの位置"), ("name", "トラックの名前"),
                               ("kind", "伴奏／ボーカルの扱い")) if old.get(k) != t.get(k)]
    if moved:
        what.append("トラックの順番")
    if not s.ara:
        what.extend(lab for k, lab in (("mute", "トラックのミュート"), ("solo", "トラックのソロ"),
                                        ("gain_db", "トラックの音量"), ("pan", "トラックのパン"))
                    if old.get(k) != t.get(k))
    if what:
        _record_session(s, "・".join(what), track_id, before, cur0, cur0, author,
                        include_mixer=not s.ara)
    s.save()
    reopened = _reopen_if_stale(s)
    _schedule(s)                                 # 位置が変わればガイドとの組み合わせも変わる
    return _ok(track=dict(t), reopened=reopened, session=summary(s),
               next=("analyze_take を呼ぶ（ガイドとの位置が変わった）" if reopened else None))


# ---------------------------------------------------------------- クリップを分ける・部分を消す（トラックビューのはさみ・ミュート）
def _finish_clip_edit(s, label, t, before, cur0, author, group=None):
    _record_session(s, label, t["id"], before, cur0, cur0, author, group=group)
    s.save()
    return _ok(track=dict(t), session=summary(s))


def _sec(v, name):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise SessionError("%s が数ではない" % name)
    if not np.isfinite(x):
        raise SessionError("%s が数ではない" % name)
    return x


@_tool
@_guarded
def split_track(track_id: str, sec: float, author: str = "ai") -> dict:
    """トラックのクリップを 1 か所で分ける（切れ目を足す）。取り消せる（undo「クリップを分ける」）。

    sec: **トラックの頭が 0 の秒**（編集の秒と同じ。タイムラインの秒 − offset_sec）。両端から 20 ms より内側だけ。
    切れ目は「部分」の境目になるだけで、音は変わらない。部分ごとに mute_track_range で消せる。
    切れ目と消した部分は、トラックの位置（offset_sec）を動かすと一緒に動く。
    """
    s = _session()
    before, cur0 = s.snapshot(), current_track_id()
    t = s.track(track_id)
    v = round(_sec(sec, "sec"), 6)
    dur = float(t["duration_sec"])
    if v < CUT_MIN_EDGE or v > dur - CUT_MIN_EDGE:
        raise SessionError("端に近すぎる（両端から %d ms 以上内側で分ける。長さ %.3f 秒）"
                           % (round(CUT_MIN_EDGE * 1000), dur))
    if any(abs(c - v) < CUT_EPS for c in t["cuts"]):
        raise SessionError("もう切れている: %.3f 秒" % v)
    t["cuts"] = sorted(t["cuts"] + [v])
    return _finish_clip_edit(s, "クリップを分ける", t, before, cur0, author)


@_tool
@_guarded
def join_track(track_id: str, sec: float, tolerance_sec: float = 0.05, author: str = "ai") -> dict:
    """secの近く（tolerance_sec 以内）にある切れ目をつなぐ。取り消せる（undo「クリップをつなぐ」）。

    つないだ部分は、両側が消えていれば消したまま、片側だけが消えていたら戻す（DAW で結合したときと同じ）。
    """
    s = _session()
    before, cur0 = s.snapshot(), current_track_id()
    t = s.track(track_id)
    v = _sec(sec, "sec")
    near = [c for c in t["cuts"] if abs(c - v) <= max(CUT_EPS, float(tolerance_sec))]
    if not near:
        raise SessionError("%.3f 秒の近くに切れ目が無い（list_tracks の cuts で確認）" % v)
    c = min(near, key=lambda x: abs(x - v))
    ps = pieces_of(t["cuts"], t["duration_sec"])
    i = t["cuts"].index(c)
    left, right = ps[i], ps[i + 1]
    t["cuts"] = [x for x in t["cuts"] if x != c]
    if not (covered(t["mutes"], *left) and covered(t["mutes"], *right)):
        t["mutes"] = norm_mutes(subtract_range(t["mutes"], left[0], right[1]), t["duration_sec"])
    return _finish_clip_edit(s, "クリップをつなぐ", t, before, cur0, author)


@_tool
@_guarded
def mute_track_range(track_id: str, start_sec: float, end_sec: float, mute: bool = True,
                     group: str = None, author: str = "ai") -> dict:
    """トラックの [start_sec, end_sec] を消す（mute=True）／戻す（False）。取り消せる（undo「部分のミュート」「部分を戻す」）。

    start_sec / end_sec: **トラックの頭が 0 の秒**。消した区間は再生で鳴らさない（伴奏にも効く。5 ms のフェード）。
    編集対象のボーカルでは、書き出し（export_wav）もその区間を 0 にする（前後 5 ms をフェード）。
    ピアノロールのノートの「無音」（mute_notes）とは別のもの（編集ではなくクリップの状態）。
    group: 続けて呼ぶ操作（画面のなぞって消す）で同じ値を渡すと、取り消しの履歴で 1 回にまとまる。
    """
    s = _session()
    before, cur0 = s.snapshot(), current_track_id()
    t = s.track(track_id)
    a, b = _sec(start_sec, "start_sec"), _sec(end_sec, "end_sec")
    dur = float(t["duration_sec"])
    a, b = max(0.0, min(a, b)), min(dur, max(a, b))
    if b - a < MUTE_MIN:
        raise SessionError("区間が短すぎる（%.3f〜%.3f 秒。長さ 0〜%.3f 秒の中で）" % (a, b, dur))
    if mute:
        t["mutes"] = norm_mutes(t["mutes"] + [[a, b]], dur)
    else:
        t["mutes"] = norm_mutes(subtract_range(t["mutes"], a, b), dur)
    return _finish_clip_edit(s, "部分のミュート" if mute else "部分を戻す", t, before, cur0, author,
                             group=group)


@_tool
@_guarded
def set_guide_track(track_id: str = None, author: str = "ai") -> dict:
    """ガイドのトラックを指定する（1 本だけ）。省略（null）でガイドを外す。取り消せる（undo）。

    編集対象のトラックには、ガイドのトラックをタイムライン上の位置を合わせて重ねる
    （list_deviations / correct_to_guide / 画面の「ガイドに合わせる」はこれを見る）。
    変えたら analyze_take を呼ぶ（ガイドの解析と対応付けが走る）。
    """
    s = _session()
    before, cur0 = s.snapshot(), current_track_id()
    prev = s.guide
    if track_id:
        t = s.track(track_id)
        if t["kind"] != "vocal":
            raise SessionError("伴奏のトラックはガイドにできない")
        s.guide = track_id
    else:
        s.guide = None
    _record_session(s, "ガイドの指定", track_id or prev, before, cur0, cur0, author)
    s.save()
    reopened = _reopen_if_stale(s)
    _schedule(s)                                 # 古い組み合わせの準備はやめ、新しいガイドで入れ直す
    return _ok(guide=s.guide, reopened=reopened, session=summary(s),
               next=("analyze_take を呼ぶ" if reopened else None))


@_tool
@_guarded
def set_track_guide(track_id: str = None, guide_track_id: str = None, author: str = "ai") -> dict:
    """トラックごとのガイドを指定する。guide_track_id を省略（null）すると共通のガイド（set_guide_track）に戻す。取り消せる（undo）。

    1 つの曲で、主旋律・ハモリ・囁きなどが別々のガイドへ合わせるときに使う（それぞれのトラックにガイドのトラックを指定する）。
    track_id: 指定されるトラック（省略時は編集対象）。ボーカルのトラックだけ。guide_track_id: ガイドにするボーカルのトラック
    （track_id 自身は不可）。実効のガイド = そのトラックの guide_id、無ければ共通のガイド。
    list_deviations / correct_to_guide / measure_against_guide / plan_edit(op="guide") / 画面のガイドの表示は、
    編集対象のトラックの実効のガイドを見る。編集対象のトラックを変えたら（返り値の reopened）analyze_take を呼ぶ
    （ガイドの解析と対応付けが走る）。
    """
    s = _session()
    before, cur0 = s.snapshot(), current_track_id()
    tid = track_id or cur0
    if not tid:
        raise SessionError("track_id が要る（編集対象のトラックが無い）")
    t = s.track(tid)
    if t["kind"] != "vocal":
        raise SessionError("伴奏のトラックにはガイドを指定できない: %s" % t["name"])
    if guide_track_id:
        g = s.track(guide_track_id)
        if g["id"] == t["id"]:
            raise SessionError("トラック自身はガイドにできない: %s" % t["name"])
        if g["kind"] != "vocal":
            raise SessionError("伴奏のトラックはガイドにできない: %s" % g["name"])
        set_guide_id(t, g["id"])
    else:
        set_guide_id(t, None)
    _record_session(s, "ガイドの指定", t["id"], before, cur0, cur0, author)
    s.save()
    reopened = _reopen_if_stale(s)
    _schedule(s)                                 # 古い組み合わせの準備はやめ、新しいガイドで入れ直す
    return _ok(track=t["id"], guide_id=t.get("guide_id"), effective_guide_id=s.effective_guide_id(t),
               reopened=reopened, session=summary(s),
               next=("analyze_take を呼ぶ" if reopened else None))


@_tool
@_guarded
def make_score_guide(path: str, track: int | str = None, bpm: float = None, start_sec: float = None,
                     use: bool = True, name: str = None, author: str = "ai") -> dict:
    """譜面（MIDI / SVP）からガイドの音を作り、トラックとして足す（ガイドの WAV が無い・息や囁きで音程が取れないとき）。
    取り消せる（undo でトラックごと消える）。

    編集対象のトラックの解析（F0・発音の頭）を使って、譜面の**時間の頭**（譜面の 0 拍がタイムラインの何秒か）と
    **トラック**（主旋律・ハモリ）を推定し、ノートどおりの高さの合成音の WAV（セッションのフォルダの
    `score-guides/`）と、そのノートの印（WAV の横の `.score.json`）を書く。印のある WAV をガイドにすると、
    ガイドのノート・発音の頭は譜面のノートそのもの、対応付けは同じ時間軸（ずれ 0）として扱う。

    track: 譜面のトラック（番号か名前）。省略するとテイクの高さに最も合うトラック（返り値の candidates）。
    bpm: テンポ。省略時は譜面のテンポ。テンポの無い MIDI はセッションのテンポ（set_tempo）、それも無ければ 120。
    start_sec: 譜面の 0 拍のタイムライン上の秒。省略すると推定する（返り値の start_sec・fit）。
    use: True ならガイドに指定する。name: トラックの名前（省略時は「譜面ガイド <ファイル名> <トラック>」）。
    変えたら analyze_take を呼ぶ（ガイドの解析と対応付けが走る）。
    """
    from . import score_guide as SG
    from . import score_import
    s = _session()
    p = _srv._project()
    p.reload_if_changed()
    p.ensure_analyzed()
    if not os.path.exists(path):
        raise SessionError("譜面が見つからない: %s" % path)
    score = score_import.read_score(path)
    dur = max([float(t["offset_sec"]) + float(t["duration_sec"]) for t in s.tracks] or [p.duration_sec])
    tempo = s.tempo or {}
    notes, start, y, info = SG.build(score, p.take_f0, current_offset_sec(), p.onsets("take"), dur,
                                     track=track, bpm=bpm, session_bpm=tempo.get("bpm"), start_sec=start_sec)
    tr = info["track"]
    key = hashlib.sha1(json.dumps([os.path.abspath(path), os.path.getsize(path), os.path.getmtime(path),
                                   tr["index"], info["bpm"], info["start_sec"], round(dur, 3)]).encode()
                       ).hexdigest()[:8]
    stem = os.path.splitext(os.path.basename(path))[0]
    wav = os.path.join(s.dir, "score-guides", "%s-t%d-%s.wav" % (stem, tr["index"], key))
    if not os.path.exists(wav) or SG.read(wav) is None:
        SG.write(wav, y, info["played"], info)
    r = add_track(wav, kind="vocal", name=name or "譜面ガイド %s %s" % (stem, tr["index"]), guide=bool(use),
                  author=author)
    if not r.get("ok"):
        return r
    out = {k: v for k, v in info.items() if k != "played"}
    return _ok(path=wav, score_track=tr, start_sec=out["start_sec"], bpm=out["bpm"],
               tempo_source=out["tempo_source"], fit=out["fit"], candidates=out["candidates"],
               warnings=out["warnings"], notes=len(info["played"]), track=r.get("track"),
               guide=s.guide, reopened=r.get("reopened"), session=r.get("session"),
               next="analyze_take を呼ぶ（ガイドが変わった）" if use else "set_guide_track でガイドにする")


# ---------------------------------------------------------------- テンポ（issue #18）
@_tool
@_guarded
def set_tempo(bpm: float = None, numerator: int = None, denominator: int = None,
              start_sec: float = None, clear: bool = False, group: str = None,
              author: str = "ai") -> dict:
    """曲のテンポと拍子（画面の時間グリッド・スナップ・ルーラーの小節と拍）を変える（渡したものだけ）。取り消せる（undo）。

    bpm: 4 分音符の数／分（20〜400）。numerator / denominator: 拍子（1〜16 / 2・4・8・16）。
    start_sec: 1 小節目の頭（タイムラインの秒。負も可）。テンポがまだ無いときは 120 BPM・4/4・0 秒から変える。
    clear: True ならテンポを消す（画面は秒のグリッドに戻る）。
    group: 続けて変える操作（画面のドラッグ・ホイール）で同じ値を渡すと、取り消しの履歴で 1 回にまとまる。
    伴奏などの WAV の iXML（Fender Studio Pro のテンポマップ）があれば、トラックを足した・開いたときに
    自動で読む（返り値の tempo.source = "ixml"）。手で変えると "manual" になる。音は変わらない。
    """
    from .project.session import TEMPO_BPM_RANGE, TEMPO_DENS, norm_tempo
    s = _session()
    before, cur0 = s.snapshot(), current_track_id()
    old = dict(s.tempo) if s.tempo else None
    if clear:
        s.tempo = None
        label = "テンポを消す"
    else:
        t = dict(old or {"bpm": 120.0, "num": 4, "den": 4, "start_sec": 0.0})
        if bpm is not None:
            v = float(bpm)
            lo, hi = TEMPO_BPM_RANGE
            if not (np.isfinite(v) and lo <= v <= hi):
                raise SessionError("bpm は %g〜%g" % (lo, hi))
            t["bpm"] = v
        if numerator is not None:
            if not (1 <= int(numerator) <= 16):
                raise SessionError("numerator（拍子の分子）は 1〜16")
            t["num"] = int(numerator)
        if denominator is not None:
            if int(denominator) not in TEMPO_DENS:
                raise SessionError("denominator（拍子の分母）は 2・4・8・16")
            t["den"] = int(denominator)
        if start_sec is not None:
            v = float(start_sec)
            if not np.isfinite(v):
                raise SessionError("start_sec が数ではない")
            t["start_sec"] = v
        t["source"] = "manual"
        for k in ("track", "file", "varies", "bpm_range"):
            t.pop(k, None)
        s.tempo = norm_tempo(t)
        sig = (numerator is not None or denominator is not None)
        label = ("テンポと拍子" if (bpm is not None or start_sec is not None) and sig
                 else "拍子" if sig else "テンポ" if bpm is not None else "1 小節目の位置")
        if old is None and bpm is None and not sig and start_sec is None:
            label = "テンポ"
    _record_session(s, label, cur0, before, cur0, cur0, author, group=group)
    s.save()
    return _ok(tempo=s.tempo, session=summary(s))


# ---------------------------------------------------------------- 取り消しの履歴（issue #16）
def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _session_of(p):
    """プロジェクト p が編集対象のトラックのものなら (セッション, トラック id)。違えば (None, None)。"""
    s = _srv._state.get("session")
    tid = current_track_id()
    if s is None or not tid or p is None:
        return None, None
    s.reload_if_changed()
    try:
        t = s.track(tid)
        if not t.get("project_dir") or _norm(p.dir) != _norm(s.project_dir_of(t)):
            return None, None
    except ProjectError:
        return None, None
    return s, tid


def has_history():
    s, _ = _session_of(_srv._state.get("project"))
    return s is not None


def _discard_dropped(s, dropped):
    """やり直しの列から捨てた項目の changeset を、プロジェクトでも「捨てた」にする（プロジェクト単位の redo で戻さない）。"""
    cur = _srv._state.get("project")
    for e in dropped:
        if e.get("kind") != "edit":
            continue
        try:
            t = s.track(e["track"])
            pdir = s.project_dir_of(t)
        except ProjectError:
            continue
        p = cur if cur is not None and _norm(cur.dir) == _norm(pdir) else None
        try:
            if p is None:
                if not os.path.exists(os.path.join(pdir, "project.json")):
                    continue
                p = Project(pdir).load()
            for cid in e.get("changesets", []):
                c = p.find_changeset(cid)
                if c is not None and c.undone and not c.discarded:
                    p.discard(cid)
        except Exception as ex:                  # noqa: BLE001  履歴は進める
            log.get().warning("やり直しの列の changeset を捨てられない: %s", ex)


def record_edit(p, cs, label, group=None):
    """編集のツールが入れた changeset を曲の履歴に足す（セッションが無い・別のプロジェクトなら何もしない）。"""
    if cs is None:
        return None
    s, tid = _session_of(p)
    if s is None:
        return None
    s.sync_project(tid, p, exclude={cs.id})
    e, dropped = s.record_edit(tid, cs.id, label, group=group, author=cs.author)
    _discard_dropped(s, dropped)
    s.save()
    return e


def forget_changeset(p, changeset_id):
    """ポップアップの当て直しで changeset を捨てた: 履歴の項目からも外す。"""
    s, tid = _session_of(p)
    if s is None or s.history is None:
        return
    s.forget_changeset(tid, changeset_id)
    s.save()


def mark_changeset(p, changeset_id, undone):
    """changeset を id で取り消した・やり直した（undo(changeset_id)）: 履歴の項目の状態を合わせる。"""
    s, tid = _session_of(p)
    if s is None:
        return
    s.ensure_history()
    e = s.entry_of(tid, changeset_id)
    if e is None:
        return
    if undone:
        e["undone"] = all(bool(getattr(p.find_changeset(c), "undone", True)) for c in e["changesets"])
    else:
        e["undone"] = False
    s.save()


def _record_session(s, label, track_id, before, cur_before, cur_after, author, group=None,
                    include_mixer=False):
    e, dropped = s.record_session(label, track_id, before, s.snapshot(), cur_before, cur_after,
                                  author=author, group=group, include_mixer=include_mixer)
    _discard_dropped(s, dropped)
    return e


def _history_switch(s, e, prefer):
    """取り消し・やり直しの後の編集対象: 操作したトラックがボーカルで今と違えばそこへ。今のトラックが
    無くなった・伴奏になったら prefer（操作の前／後の編集対象）か最初のボーカルへ。切り替えた id か None。"""
    cur = current_track_id()
    ids = {t["id"]: t for t in s.tracks}
    t = ids.get(e.get("track"))
    target = None
    if t is not None and t["kind"] == "vocal" and t["id"] != cur and t.get("project_dir"):
        target = t
    elif cur not in ids or ids[cur]["kind"] != "vocal":
        p = ids.get(prefer)
        target = p if p is not None and p["kind"] == "vocal" else (s.vocal_tracks() or [None])[0]
    if target is None:
        return None
    _open_track(s, target)
    return target["id"]


def _history_result(s, e, switched, reopened, key):
    p = _srv._state.get("project")
    brief = {"id": e["id"], "label": e.get("label"), "kind": e.get("kind"),
             "track": e.get("track"), "author": e.get("author")}
    try:
        brief["track_name"] = s.track(e.get("track"))["name"]
    except ProjectError:
        brief["track_name"] = None
    h = s.history_summary()
    return {key: brief, "switched_to": switched, "reopened": bool(reopened or switched),
            "total_edits": len(p.edits) if p is not None else 0,
            "can_undo": h["can_undo"], "can_redo": h["can_redo"], "history": h,
            "session": summary(s),
            "next": ("analyze_take を呼ぶ（編集対象が変わった）" if switched
                     else "analyze_take を呼ぶ（ガイドとの位置が変わった）" if reopened else None)}


def _changeset_has_lyrics(p, ids):
    for cid in ids:
        c = p.find_changeset(cid)
        if c is not None and any(o.get("op") == "lyrics" for o in c.ops):
            return True
    return False


def _cs_undone(s, e):
    """項目の changeset がプロジェクトでいま取り消し済みか [bool]（見つからないものは True）。"""
    cur = _srv._state.get("project")
    try:
        pdir = s.project_dir_of(s.track(e["track"]))
    except ProjectError:
        return [True]
    if cur is not None and _norm(cur.dir) == _norm(pdir):
        cur.reload_if_changed()
        p = cur
    else:
        if not os.path.exists(os.path.join(pdir, "project.json")):
            return [True]
        p = Project(pdir).load()
    out = []
    for cid in e.get("changesets", []):
        c = p.find_changeset(cid)
        out.append(True if c is None else bool(c.undone))
    return out


def _skip_stale(s, undo):
    """外で（別のプロセス・project.json の直接の書き換えで）もう取り消された／やり直された項目は、
    状態だけ合わせて飛ばす（Ctrl+Z 1 回で何も起きない、にしない）。"""
    changed = False
    while True:
        e = s.undo_target() if undo else s.redo_target()
        if e is None or e.get("kind") != "edit":
            break
        st = _cs_undone(s, e)
        if undo and all(st):
            e["undone"] = True
        elif not undo and not any(st):
            e["undone"] = False
        else:
            break
        changed = True
    if changed:
        s.save()


def _restore_archive_history(s, e, side):
    """明示的な ARA 取り込みの前後へ戻す。補正と解析方式を一緒に復元する。"""
    from . import mcp_ara as ara

    t = s.track(e["track"])
    arc = e[side]
    est = arc.get("f0_estimator")
    if arc.get("changesets") and est:
        why = ara._estimator_problem(est)
        if why:
            raise ProjectError("補正を復元できない: %s" % why)
    cur = _srv._state.get("project")
    old_pref = t.get("estimator")
    old_explicit = t.get(EXPLICIT_KEY)
    old_mark = s.history_marks.get(t["id"])
    try:
        with _recover_projects([s.project_dir_of(t)]):
            p = ara._restore_into(s, t, arc, forget_history=False)
            pref = e.get("estimator_" + side)
            if pref is None:
                t.pop("estimator", None)
            else:
                t["estimator"] = pref
            t.pop(EXPLICIT_KEY, None)            # 取り込んだアーカイブの方式（利用者の明示ではない）
            p.estimator_pref = pref or est
            if p.edits and not p.analysis_cached(est):
                with prep.exclusive(p.dir):
                    p.analyze(estimator=est)
            s.history_marks[t["id"]] = int(e.get("mark_" + side) or 0)
    except BaseException:
        if old_pref is None:
            t.pop("estimator", None)
        else:
            t["estimator"] = old_pref
        if old_explicit:
            t[EXPLICIT_KEY] = old_explicit
        if old_mark is None:
            s.history_marks.pop(t["id"], None)
        else:
            s.history_marks[t["id"]] = old_mark
        if cur is not None and _norm(cur.dir) == _norm(s.project_dir_of(t)):
            cur.load()
            cur._forget_analysis()
        raise
    if cur is not None and cur is not p and _norm(cur.dir) == _norm(p.dir):
        cur.load()
        cur._forget_analysis()
        cur.estimator_pref = pref or est
    _srv._invalidate_renderer()
    ara._drop_render(t["ara_id"])
    _schedule(s)
    return t["id"]


def history_undo():
    """曲の履歴の最後の操作を取り消す。別のトラックの操作なら、そのトラックを編集対象にしてから。"""
    s, tid = _session_of(_srv._state.get("project"))
    if s is None:
        raise ProjectError("取り消しの履歴が無い（open_project で開いたセッションが無い）")
    s.ensure_history()
    s.sync_project(tid, _srv._state["project"])
    _skip_stale(s, undo=True)
    e = s.undo_target()
    if e is None:
        raise ProjectError("取り消せる変更が無い")
    switched, reopened = None, False
    if e.get("kind") == "estimator":
        _commit_estimator_history(s, e, e["before"], True)
    elif e.get("kind") == "archive":
        tid = _restore_archive_history(s, e, "before")
        e["undone"] = True
        s.save()
        if tid != current_track_id():
            switched = _history_switch(s, e, None)
    elif e.get("kind") == "edit":
        t = s.track(e["track"])
        if t["id"] != current_track_id():
            _open_track(s, t)
            switched = t["id"]
        p = _srv._state["project"]
        p.reload_if_changed()
        for cid in reversed(e.get("changesets", [])):
            c = p.find_changeset(cid)
            if c is not None and not c.undone:
                p.undo(cid)
        if _changeset_has_lyrics(p, e.get("changesets", [])):
            _srv._invalidate_renderer()
        s.reload_if_changed()
        e = next((x for x in s.history if x["id"] == e["id"]), e)
        e["undone"] = True
        s.save()
    else:
        s.restore(e["before"], include_mixer=bool(e.get("include_mixer")), other=e.get("after"))
        e["undone"] = True
        s.save()
        switched = _history_switch(s, e, e.get("current_before"))
        if switched is None:
            reopened = _reopen_if_stale(s)
        _schedule(s)
    log.get().info("曲の履歴を取り消した: %s（%s・%s）", e["id"], e.get("label"), e.get("track"))
    return _history_result(s, e, switched, reopened, "undone")


def history_redo():
    """曲の履歴の、直近に取り消した操作をやり直す。"""
    s, tid = _session_of(_srv._state.get("project"))
    if s is None:
        raise ProjectError("取り消しの履歴が無い（open_project で開いたセッションが無い）")
    s.ensure_history()
    _skip_stale(s, undo=False)
    e = s.redo_target()
    if e is None:
        raise ProjectError("やり直せる変更が無い")
    switched, reopened = None, False
    if e.get("kind") == "estimator":
        _commit_estimator_history(s, e, e["after"], False)
    elif e.get("kind") == "archive":
        tid = _restore_archive_history(s, e, "after")
        e["undone"] = False
        s.save()
        if tid != current_track_id():
            switched = _history_switch(s, e, None)
    elif e.get("kind") == "edit":
        t = s.track(e["track"])
        if t["id"] != current_track_id():
            _open_track(s, t)
            switched = t["id"]
        p = _srv._state["project"]
        p.reload_if_changed()
        for cid in e.get("changesets", []):
            c = p.find_changeset(cid)
            if c is not None and c.undone and not c.discarded:
                p.redo(cid)
        if _changeset_has_lyrics(p, e.get("changesets", [])):
            _srv._invalidate_renderer()
        s.reload_if_changed()
        e = next((x for x in s.history if x["id"] == e["id"]), e)
        e["undone"] = False
        s.save()
    else:
        s.restore(e["after"], include_mixer=bool(e.get("include_mixer")), other=e.get("before"))
        e["undone"] = False
        s.save()
        switched = _history_switch(s, e, e.get("current_after"))
        if switched is None:
            reopened = _reopen_if_stale(s)
        _schedule(s)
    log.get().info("曲の履歴をやり直した: %s（%s・%s）", e["id"], e.get("label"), e.get("track"))
    return _history_result(s, e, switched, reopened, "redone")


def history_summary():
    s, _ = _session_of(_srv._state.get("project"))
    if s is None:
        return None
    s.ensure_history()
    return s.history_summary()


# ---------------------------------------------------------------- 画面の描画・再生
def _sha_of(t):
    return t.get("sha256") or hashlib.sha256(t["path"].encode("utf-8")).hexdigest()


def _clip_frames(t):
    sr = int(t["sr"])
    c = t.get("clip")
    if not c:
        return 0, int(t["source_frames"])
    return int(round(float(c["offset_sec"]) * sr)), int(round(float(c["length_sec"]) * sr))


@_tool
def track_overview(track_ids: list = None) -> dict:
    """**画面（トラックビュー）向け**: 元音声の各チャンネルの符号付き min/max を
    32/128/512/2048/8192 サンプル刻みの Int8 バイナリに書き、JSON メタのパスを返す。
    LLM が読む必要はない。素材と切り出し範囲ごとにキャッシュする。"""
    s = _session()
    ids = track_ids or [t["id"] for t in s.tracks]
    d = os.path.join(s.dir, "overview")
    os.makedirs(d, exist_ok=True)
    out = []
    for tid in ids:
        t = s.track(tid)
        off, n = _clip_frames(t)
        sr = int(t["sr"])
        key = "v%d-%s-o%d-n%d" % (OVERVIEW_VERSION, _sha_of(t)[:16], off, n)
        path = os.path.join(d, key + ".json")
        binary = os.path.join(d, key + ".bin")
        try:
            if not os.path.exists(path) or not os.path.exists(binary):
                _write_overview(t["path"], off, n, sr, path)
        except Exception as e:                   # noqa: BLE001  このトラックだけ波形なし
            out.append({"id": tid, "error": str(e)})
            continue
        out.append({"id": tid, "path": path, "binary_path": binary})
    return _ok(tracks=out, hint="UI 向けの JSON メタと Int8 バイナリ。配列は結果に入れていない")


def _write_overview(src, off, n, sr, path):
    hop = OVERVIEW_HOPS[0]
    mins, maxs = [], []
    carry = None
    channels = sf.info(src).channels
    for block in sf.blocks(src, blocksize=hop * 4096, start=off, stop=off + n,
                           dtype="float32", always_2d=True):
        if carry is not None:
            block = np.concatenate((carry, block))
        k = len(block) // hop
        carry = block[k * hop:] if k * hop < len(block) else None
        if k:
            shaped = block[:k * hop].reshape(k, hop, channels)
            mins.append(shaped.min(axis=1))
            maxs.append(shaped.max(axis=1))
    if carry is not None and len(carry):
        mins.append(carry.min(axis=0, keepdims=True))
        maxs.append(carry.max(axis=0, keepdims=True))
    lo = np.concatenate(mins) if mins else np.zeros((1, channels), dtype="float32")
    hi = np.concatenate(maxs) if maxs else np.zeros((1, channels), dtype="float32")
    levels = []
    binary = path[:-5] + ".bin"
    tmp_binary = binary + ".tmp"
    with open(tmp_binary, "wb") as f:
        for level_hop in OVERVIEW_HOPS:
            count = len(lo)
            levels.append({"hop": level_hop, "length": count, "offset": f.tell()})
            # Each bin is [channel 0 min, max, channel 1 min, max, ...].
            pair = np.empty((count, channels, 2), dtype="int8")
            pair[:, :, 0] = np.clip(np.floor(lo * 127), -127, 127).astype("int8")
            pair[:, :, 1] = np.clip(np.ceil(hi * 127), -127, 127).astype("int8")
            f.write(pair.tobytes())
            if level_hop != OVERVIEW_HOPS[-1]:
                groups = (count + 3) // 4
                pad = groups * 4 - count
                lo = np.pad(lo, ((0, pad), (0, 0)), constant_values=np.inf).reshape(groups, 4, channels).min(axis=1)
                hi = np.pad(hi, ((0, pad), (0, 0)), constant_values=-np.inf).reshape(groups, 4, channels).max(axis=1)
    os.replace(tmp_binary, binary)
    data = {"version": OVERVIEW_VERSION, "sr": sr, "channels": channels,
            "duration_sec": n / sr, "binary": os.path.basename(binary), "levels": levels}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


STEM_VERSION = 1                # 再生用の音の作り方を変えたら上げる


def _edits_rev(p, backend):
    """編集を当てた音の版（編集リスト・素材・解析・作り方）。どれかが変われば作り直す。"""
    h = hashlib.sha1()
    tk = p.take or {}
    h.update(json.dumps([STEM_VERSION, tk.get("sha256"), tk.get("offset_frames"), tk.get("frames"),
                         (p.analysis.get("take") or {}).get("analyzed_at"),
                         (p.analysis.get("take") or {}).get("estimator")],
                        sort_keys=True).encode("utf-8"))
    h.update(json.dumps([e.to_json() for e in p.edits], sort_keys=True,
                        ensure_ascii=False).encode("utf-8"))
    h.update(str(backend).encode())
    return h.hexdigest()[:12]


def _track_project(s, t):
    """トラックのプロジェクト（編集対象ならそれ。他は読むだけ。まだ無ければ None）。"""
    pdir = s.project_dir_of(t)
    cur = _srv._state.get("project")
    if cur is not None and os.path.normcase(cur.dir) == os.path.normcase(pdir):
        cur.reload_if_changed()
        return cur, True
    if not os.path.exists(os.path.join(pdir, "project.json")):
        return None, False
    return Project(pdir).load(), False


def _prune_stems(d, keep):
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.startswith("stem-")),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        return
    for f in files[keep:]:
        try:
            os.remove(f)
        except OSError:
            pass


def _stem(s, t, backend):
    """トラックの音（ファイル）: 編集があれば当てた音（長さ・チャンネルは元のまま）、無ければ元のファイル。"""
    base = {"id": t["id"], "start_sec": float(t["offset_sec"]),
            "duration_sec": float(t["duration_sec"])}
    if t["kind"] == "vocal" and t.get("project_dir"):
        p, is_cur = _track_project(s, t)
        if p is not None and p.edits and os.path.exists(p.take["path"]):
            from .render.region import RegionRenderer, render_region
            rev = _edits_rev(p, backend)
            d = p.sub("renders")
            path = os.path.join(d, "stem-%s.wav" % rev)
            if not os.path.exists(path):
                rr = None
                from .render.base import resolve_backend_name
                key = (resolve_backend_name(backend), "all")
                if is_cur:
                    rr = _srv._state["region"].get(key)
                if rr is None:
                    rr = RegionRenderer.for_project(p, backend=backend, channels="all")
                    if is_cur:
                        _srv._state["region"][key] = rr
                y, info = render_region(p, 0.0, p.duration_sec, renderer=rr)
                tmp = path + ".tmp.wav"
                sf.write(tmp, y.astype("float32"), info["sr"], subtype="FLOAT")
                os.replace(tmp, path)
                _prune_stems(d, KEEP_STEMS)
                log.get().info("トラックの音を作った: %s（%d 窓）", path,
                               len(info["rendered_windows_sec"]))
            else:
                os.utime(path, None)
            return dict(base, path=os.path.abspath(path), rev=rev, edited=True)
    src = t["path"]
    if not os.path.exists(src):
        raise SessionError("音声が見つからない: %s" % src)
    off, n = _clip_frames(t)
    whole = off == 0 and n == int(t["source_frames"])
    if whole and os.path.splitext(src)[1].lower() in PLAYABLE_EXTS:
        return dict(base, path=os.path.abspath(src), rev="orig", edited=False)
    # クリップ（ファイルの一部）・画面がそのまま読めない形式: 範囲を WAV に書いて渡す
    d = os.path.join(s.dir, "mix")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s-o%d-n%d.wav" % (_sha_of(t)[:16], off, n))
    if not os.path.exists(path):
        x, sr = sf.read(src, start=off, stop=off + n, dtype="float32", always_2d=True)
        tmp = path + ".tmp.wav"
        sf.write(tmp, x, sr, subtype="FLOAT")
        os.replace(tmp, path)
    return dict(base, path=os.path.abspath(path), rev="orig", edited=False)


@_tool
def render_tracks(track_ids: list = None, backend: str = "praat", background: bool = None) -> dict:
    """**画面（再生）向け**: トラックごとの音のファイルを返す（画面が位置・ミュート／ソロを当てて混ぜて鳴らす）。

    編集のあるボーカルのトラックは編集を当てた音（`render_region` と同じ中身。長さ・チャンネルは元のまま。
    編集が変わるまで使い回す）、それ以外は元のファイルそのもの。返り値の tracks[].start_sec が
    タイムライン上の位置（= offset_sec）。LLM が聴く用途なら render_preview を使うこと。
    """
    s = _session()
    ids = track_ids or [t["id"] for t in s.tracks]
    tracks = [s.track(i) for i in ids]
    if background is None:
        # 作り直しが要るトラックの長さの合計で決める（編集が変わっていなければファイルを返すだけ）
        need = 0.0
        for t in tracks:
            if t["kind"] != "vocal" or not t.get("project_dir"):
                continue
            p, _ = _track_project(s, t)
            if p is not None and p.edits:
                if not os.path.exists(os.path.join(p.dir, "renders",
                                                   "stem-%s.wav" % _edits_rev(p, backend))):
                    need += float(t["duration_sec"])
        background = need * 0.15 > _srv.JOB_THRESHOLD_SEC

    def work():
        out = []
        for t in tracks:
            try:
                with prep.foreground():          # 音を作る間、裏の準備は次の段へ進まない（issue #63）
                    out.append(_stem(s, t, backend))
            except Exception as e:               # noqa: BLE001  このトラックだけ鳴らさない
                log.get().warning("トラック %s の音を作れない: %s", t["id"], e)
                out.append({"id": t["id"], "error": str(e), "start_sec": float(t["offset_sec"])})
        return {"tracks": out, "session": summary(s)}

    if background:
        return _srv._submit_job("render_tracks", work)
    return _ok(**work())


TOOLS = [list_tracks, select_track, add_track, remove_track, set_track, set_guide_track, set_track_guide, make_score_guide,
         split_track, join_track, mute_track_range, set_tempo, track_overview, render_tracks]
