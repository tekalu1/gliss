# -*- coding: utf-8 -*-
"""MCP: DAW（VST3 + ARA 2 のプラグイン）のドキュメント（`engine/docs/MCP.md` §3-4）。

プラグイン（C++）が子プロセスのエンジンを 1 本起動し、DAW のドキュメント 1 つをエンジンのセッション 1 つ
（作業場所 `work/ara/<work_key>/`）、ARA の AudioModification 1 つをセッションのボーカルのトラック 1 本にする。
修飾の範囲はソース全体（編集はソースの秒）。PlaybackRegion（DAW のイベント）はエンジンは知らない。

| ツール | 何をするか |
|---|---|
| `ara_open(work_key, name?)` | 作業場所を開く／作る（文書の種類 `ara`） |
| `ara_set_modification(ara_id, source_path, source_id, name, offset_sec?, group?, clone_of?)` | 修飾のトラックを作る／直す |
| `ara_remove_modification(ara_id)` | 修飾のトラックを外す（プロジェクトは残す） |
| `ara_sync(tracks, tempo?)` | DAW の位置・名前・テンポをまとめて当てる |
| `ara_render_dirty(ara_id, since?, backend?, channels?, max_sec?)` | 前に渡した版からの差分の再合成（PCM はファイル） |
| `ara_revs()` | 全修飾の今の版（ロックを取らない） |
| `ara_archive(ara_ids?)` | 保存用の編集リスト（ロックを取らない） |
| `ara_restore(ara_id, archive)` | アーカイブから編集を戻す（素材が違えば当てない） |
| `ara_notes(ara_ids?)` | DAW に返すノート（編集を当てた後と、解析だけのもの。ソースの秒） |
| `export_edits(gliss_path, track?, estimator?, include_archive?)` | `.gliss` のトラックの編集を ARA のアーカイブにして返す（読むだけ） |
| `import_edits(archive? / gliss_path?, track?, replace?, estimator?, analyze?)` | アーカイブを選んでいる修飾へ当てる（外部の AI から中継で呼べる編集ツール） |

共通: `author` を取らず、**取り消しの履歴に入れない**（DAW が決めたことを Gliss の Ctrl+Z で戻させない）。
画面の編集（`shift_pitch` など）・`undo` / `redo`・`select_track`・`analyze_take` は今までのツールをそのまま使う。

**差分の再合成**（`ara_render_dirty`）は `render/region.py` の `EditCache` と同じ考え方をファイルで渡す:
エンジンは修飾ごとに「最後に渡した版と、その時の Segment 列・窓」を覚え、`since` が同じなら
変わった範囲（`dirty_windows`）を原音に戻させ（`restore`）、そこに掛かる窓だけを再合成して渡す（`windows`）。
プラグインは原音の上に順に当てるだけで、結果は `render_region` の全体（＝書き出し）とサンプル単位で同じ。
`max_sec` を超える分は次の呼び出しに回す（`more`。返す版は途中の印 `<版>~<n>`。`_lock` を長く握らない）。
"""
import collections
import copy
import hashlib
import json
import os
import time

import numpy as np

from . import log
from . import media as M
from . import mcp_document as _md
from . import mcp_server as _srv
from . import mcp_tracks as _mt
from . import prep
from .audio import file_sig
from .project import Project, ProjectError
from .project import document as D
from .project.session import Session, SessionError, norm_tempo, set_guide_id
from .project import transfer as _tr
from .project.store import ARCHIVE_FORMAT, dir_lock

_ok = _srv._ok
_tool = _srv._tool

OUT_DIR = "ara-out"             # 差分の再合成の PCM（.f32）の置き場（作業場所の下）
KEEP_OUT = 4                    # 修飾ごとに残す .f32 の数（プラグインが読み終える前に消さない）
RENDERERS_MAX = 4               # 下ごしらえ（RegionRenderer）を持っておく修飾の数（LRU）
DEFAULT_MAX_SEC = 10.0          # ara_render_dirty が 1 回で再合成する窓の長さの上限（秒）
EMPTY_REV = "empty"             # プロジェクト（project.json）がまだ無い修飾の版
NOTE_FLOOR_DB = -60.0           # ara_notes の音量: この dB を 0、0 dB を 1 にする（ARA の volume は dB に近い尺度）
# アーカイブの素材の参照に入れるキー（`Project.to_archive` の ref と同じ）
_REF_KEYS = ("source_id", "source_kind", "source_name", "sha256", "sr", "channels",
             "subtype", "source_frames", "offset_frames", "frames", "path", "pad")

# 修飾ごとの差分の再合成の状態（このプロセスの中だけ。エンジンを起動し直したら since が合わず reset になる）:
# ara_id → {rev, segs, windows, pending, backend, channels, asig}
_render = {}
_renderers = collections.OrderedDict()      # ara_id → (鍵, RegionRenderer)
_out_seq = [0]


# ---------------------------------------------------------------- 共通
def _key(ara_id):
    """ara_id → ファイル名に使える短い鍵（プロジェクトのディレクトリ・.f32 の名前）。"""
    return hashlib.sha1(str(ara_id).encode("utf-8")).hexdigest()[:12]


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _doc():
    d = _md.current()
    return d if d is not None and d.kind == "ara" else None


def _session():
    s = _srv._state.get("session")
    if _doc() is None or s is None:
        raise ProjectError("DAW のドキュメントが開かれていない（先に ara_open を呼ぶ）")
    if s.reload_if_changed():
        _mt._schedule(s)
    return s


def _track(s, ara_id):
    t = s.find_ara(ara_id)
    if t is None:
        raise SessionError("修飾のトラックが無い: %s（先に ara_set_modification）" % ara_id)
    return t


def _row(s, t):
    return {"id": t["id"], "ara_id": t["ara_id"], "name": t["name"], "group": t.get("group"),
            "offset_sec": t["offset_sec"], "duration_sec": t["duration_sec"], "sr": t["sr"],
            "channels": t["channels"], "source_frames": t["source_frames"],
            "source_id": t.get("source_id"), "path": t["path"], "project_dir": s.project_dir_of(t),
            "current": t["id"] == _mt.current_track_id(), "guide": t["id"] == s.guide,
            "guide_id": t.get("guide_id"), "effective_guide_id": s.effective_guide_id(t)}


def _guides_of(s):
    """トラックごとのガイド（`set_track_guide`）→ {修飾の ara_id: ガイドの修飾の ara_id}（DAW の文書の外のトラックは入れない）。"""
    out = {}
    tracks = list(s.tracks)
    for t in tracks:
        gid = t.get("guide_id")
        if not t.get("ara_id") or not gid:
            continue
        g = next((x for x in tracks if x["id"] == gid), None)
        if g is not None and g.get("ara_id"):
            out[t["ara_id"]] = g["ara_id"]
    return out


def _guide_records(s, t):
    """外すトラック t のガイドの指定（本人の `guide_id` と、t を指していた指定）。`ara_gone` に控えて、足し直しで戻す。"""
    rec = {}
    if t.get("guide_id"):
        rec["guide_id"] = t["guide_id"]
    refs = [x["id"] for x in s.tracks if x.get("guide_id") == t["id"]]
    if refs:
        rec["guide_refs"] = refs
    return rec


def _restore_guides(s, t, gone):
    """足し直したトラック t のガイドの指定を戻す（DAW の取り消し）。相手がまだ戻っていない組は
    `s.ara_guide_wait`（トラック id → ガイドのトラック id）に預け、相手が戻ったときに当てる。
    今のトラックに別の指定が入っていれば上書きしない。"""
    wait = s.ara_guide_wait
    pairs = []
    if gone.get("guide_id"):
        pairs.append((t["id"], gone["guide_id"]))
    pairs.extend((r, t["id"]) for r in gone.get("guide_refs") or [])
    pairs.extend(wait.items())
    by_id = {x["id"]: x for x in s.tracks}
    for src, dst in pairs:
        a, b = by_id.get(src), by_id.get(dst)
        if a is None or b is None:
            wait[src] = dst                      # 相手がまだ戻っていない
            continue
        wait.pop(src, None)
        if a["kind"] == "vocal" and b["kind"] == "vocal" and src != dst and not a.get("guide_id"):
            set_guide_id(a, dst)


def _audio_sha(path):
    """ソース全体の音の中身のハッシュ（`media.clip_audio_hash` と同じ値。書式・ファイルのバイトによらない）。"""
    return M.clip_hash_of(M.Clip(os.path.abspath(path)))


def _sig(path):
    s = file_sig(path)
    return list(s) if s is not None else None


def _cs_num(cid):
    try:
        return int(str(cid).lstrip("c"))
    except ValueError:
        return 0


def _forget_history(s, tid):
    """トラックの編集の項目を取り消しの履歴から外す（DAW が外した・アーカイブで中身を入れ替えた）。

    履歴に残すと、Ctrl+Z が無いトラック・別の中身の changeset を戻そうとする。`history_marks` は残す
    （同じ changeset を後で履歴の末尾に足し直さない）。"""
    s.ensure_history()
    s.history = [e for e in s.history if not (e.get("kind") == "edit" and e.get("track") == tid)]


def _mark_all(s, tid, p):
    """プロジェクトの今の changeset を「履歴に入れた」ことにする（アーカイブから戻した編集を Ctrl+Z の列に足さない）。"""
    s.ensure_history()
    n = max([_cs_num(c.id) for c in p.changesets] + [int(s.history_marks.get(tid) or 0)])
    if n:
        s.history_marks[tid] = n


_fit_tried = set()                          # (ara_id, 今の方式, 当たらないノートの ID) — 方式を探して見つからなかった組み合わせ


def _drop_render(ara_id):
    _render.pop(ara_id, None)
    _renderers.pop(ara_id, None)


def _reset_render():
    _render.clear()
    _renderers.clear()
    _fit_tried.clear()


def _analyzed(s, t):
    p = _srv._state.get("project")
    if t["id"] == _mt.current_track_id() and p is not None:
        return _mt.analyzed(s, p)
    try:
        return bool(prep.is_ready(s, t))
    except Exception:                            # noqa: BLE001
        return False


def _close_project():
    """編集対象のトラックが無くなった（ボーカルが 0 本）: 開いているプロジェクトを手放す。"""
    old = _srv._state.get("project")
    if old is not None:
        from .project.pitch import forget_cache
        forget_cache(old)
    _srv._state.update(project=None, track=None)
    _srv._invalidate_renderer()


# ---------------------------------------------------------------- 素材
def _repoint(s, t, content_changed):
    """トラックのプロジェクト（project.json）のテイクを、トラックの今の素材に合わせる。

    DAW のプラグインは開くたびにソースの WAV を書き直す（同じ音でもファイルのバイト・SHA-256 が変わりうる）。
    そのまま `Project.open` すると「素材が変わった」と見て編集を捨てて作り直すので、先にテイクの参照を直す。
    content_changed: 音そのものが変わった（DAW で差し替えた）。編集は残し、解析のキャッシュは捨てる。"""
    pdir = s.project_dir_of(t)
    if not os.path.exists(os.path.join(pdir, "project.json")):
        return False
    try:
        info = M.describe(M.Clip(t["path"], source_id=t.get("source_id")), t["path"],
                          sha256=t["sha256"], role="take")
    except M.MediaError as e:
        raise ProjectError(str(e))
    with dir_lock(pdir):
        q = Project(pdir).load()
        old = q.take or {}
        if (not content_changed and old.get("sha256") == info["sha256"]
                and _norm(old.get("path") or "") == _norm(info["path"])
                and old.get("source_id") == info["source_id"]):
            return False
        q.take = info
        if content_changed:
            q._clear_cache()
            q.analysis = {}
        q.save()
    log.get().info("修飾 %s のテイクの参照を直した（%s）: %s", t.get("ara_id"),
                   "音が変わった" if content_changed else "同じ音", pdir)
    return True


def _refresh_source(s, t, path, source_id):
    """既存のトラックの素材を path に合わせる。(ファイルが変わった, 音が変わった)。"""
    path = os.path.abspath(path)
    sig = _sig(path)
    sid = source_id or t.get("source_id")
    if (_norm(t["path"]) == _norm(path) and t.get("ara_file_sig") == sig and t.get("ara_audio_sha")
            and sid == t.get("source_id")):
        return False, False
    h = _audio_sha(path)
    changed = bool(t.get("ara_audio_sha")) and h != t["ara_audio_sha"]
    info = M.file_info(path)
    from .audio import sha256_file
    t.update(path=path, sha256=sha256_file(path), sr=int(info.samplerate), channels=int(info.channels),
             source_frames=int(info.frames), duration_sec=round(int(info.frames) / int(info.samplerate), 6),
             clip=None, source_id=sid, ara_file_sig=sig, ara_audio_sha=h)
    _repoint(s, t, changed)
    return True, changed


# ---------------------------------------------------------------- アーカイブ
def _ref(m, clip_hash=None):
    if not m:
        return None
    d = {k: m.get(k) for k in _REF_KEYS}
    d["clip_audio_sha256"] = clip_hash or M.clip_audio_hash(m)
    return d


def _archive_of(t, pdir):
    """トラックの編集の状態（`Project.to_archive` と同じ形）。**ロックを取らず**ディスクの project.json から作る。

    素材の照合のハッシュはトラックを作った・素材が変わったときに覚えた値を使う（毎回ソースを読まない）。
    ガイドは入れない（ガイドはセッションの指定で、DAW の別の修飾）。プロジェクトがまだ無ければ None。"""
    if not os.path.exists(os.path.join(pdir, "project.json")):
        return None
    q = Project(pdir).load()
    take = q.take
    q.take = q.guide = None
    # 明示方式を先に反映し、方式名とモデル版を同じ解析先から作る。
    # 後から方式名だけ上書きすると、解析待ちの保存で Gliss 版が null になる。
    q.estimator_pref = t.get("estimator")
    arc = q.to_archive()
    known = t.get("ara_audio_sha") if take and take.get("sha256") == t.get("sha256") else None
    arc["take"] = _ref(take, known)
    arc["guide"] = None
    (arc.get("lyrics") or {}).pop("guide", None)
    return arc


def _mismatch(t, archive):
    """アーカイブの素材がトラックの素材と違えば理由（同じなら None）。形が違うアーカイブは ProjectError。"""
    if not isinstance(archive, dict) or archive.get("format") != ARCHIVE_FORMAT:
        raise ProjectError("アーカイブの形式が違う: %r" % (archive or {}).get("format"))
    tref = archive.get("take")
    if not tref:
        raise ProjectError("アーカイブにテイクの参照が無い")
    if int(tref.get("offset_frames") or 0) != 0 or int(tref.get("frames") or -1) != int(t["source_frames"]):
        return "長さが違う（アーカイブ %s サンプル・今の音 %d サンプル）" % (tref.get("frames"), int(t["source_frames"]))
    if tref.get("sha256") and tref["sha256"] == t.get("sha256"):
        return None
    if tref.get("clip_audio_sha256") and tref["clip_audio_sha256"] == t.get("ara_audio_sha"):
        return None
    return "音の中身が違う（DAW の音が変わったので編集を当てない）"


def _restore_into(s, t, archive, forget_history=True):
    """アーカイブをトラックのプロジェクトに戻す（素材は照合済み）。戻した Project。"""
    pdir = s.project_dir_of(t)
    arc = copy.deepcopy(archive)
    arc["guide"] = None
    (arc.get("lyrics") or {}).pop("guide", None)
    clip = M.Clip(t["path"], offset_frames=0, length_frames=int(t["source_frames"]),
                  source_id=t.get("source_id"))
    with dir_lock(pdir):
        p = Project.from_archive(arc, take=clip, project_dir=pdir, overwrite=True)
    if forget_history:
        _forget_history(s, t["id"])
    _mark_all(s, t["id"], p)
    return p


def _estimator_problem(est):
    """アーカイブの F0 の方式をこの PC で使えない理由（使えるなら None）。"""
    from .analysis import f0 as F
    if est not in F.ESTIMATORS:
        return "知らない F0 の方式: %r（%s）" % (est, " / ".join(F.ESTIMATORS))
    if est == "rmvpe" and not F.rmvpe_available():
        return "F0 の方式 rmvpe の重み（rmvpe.onnx）がこの PC に無い（補正を作ったときと音が変わる）"
    return None


def _apply_estimator(s, t, est):
    """アーカイブの `f0_estimator` を、トラックの方式（session.json の `estimator`）にする。(方式 | None, 理由 | None)。

    選んでいる方式と同じでも明示で持つ（保存し直したアーカイブから方式が消えない）。使えない方式（重みが無い）は
    当てずに理由を返す。呼び出し側が `s.save()` と裏の準備の入れ直しをする。"""
    if not est:
        return None, None
    why = _estimator_problem(est)
    if why:
        return None, why
    t["estimator"] = est
    t.pop(_mt.EXPLICIT_KEY, None)               # アーカイブの方式（利用者の明示ではない）
    return est, None


def _fit_estimator(s, t, p):
    """ノートの ID に頼る編集の対象が、今の方式の解析に無いとき、全部の対象が見つかる方式を探してそのトラックの方式にする。
    方式の記録の無い（古い）アーカイブ・記録が別の方式になってしまったアーカイブ（開き直しで方式が外れたまま保存された曲）を
    救う。ノートの ID は解析の方式で変わるので、補正を作った方式でなければ編集が当たらない。今の方式で全部当たるなら何もしない。
    探す（F0 の推定は方式ごとに数秒〜数十秒）のは、同じ組み合わせにつき 1 回だけ。見つかれば True（解析し直した）。

    利用者が明示した方式（`analyze_take(estimator=…)`・`set_f0_estimator(scope="current")`。トラックの
    `estimator_explicit`）は戻さない。当たらない編集は、呼び出し元が missing として報告する（`_missing_error`）。"""
    from .analysis import f0 as F
    missing = sorted({nid for _e, nid in p._missing_note_targets()})
    if not missing:
        return False
    cur = p.f0_estimator()
    key = (t["ara_id"], cur, tuple(missing))
    if key in _fit_tried:
        return False
    _fit_tried.add(key)
    if t.get(_mt.EXPLICIT_KEY):
        log.get().warning("修飾 %s: 編集の対象のノート %s が、利用者が明示した方式 %s の解析に無い（方式は戻さない）",
                          t["ara_id"], ", ".join(missing[:5]), cur)
        return False
    for cand in F.ESTIMATORS:
        if cand == cur or _estimator_problem(cand):
            continue
        try:
            lost = p.missing_note_targets_with(cand)
        except Exception as e:                   # noqa: BLE001  その方式は試せない（素材が読めないなど）
            log.get().warning("修飾 %s: F0 の方式 %s で編集の対象を調べられない: %s", t["ara_id"], cand, e)
            continue
        if lost:
            continue
        log.get().warning("修飾 %s: 編集の対象のノート %s が方式 %s の解析に無い。全部当たる %s に替える",
                          t["ara_id"], ", ".join(missing[:5]), cur, cand)
        _apply_estimator(s, t, cand)             # 方式探しで決めた方式（明示の印は付けない）
        p.estimator_pref = cand
        s.save()
        with prep.exclusive(p.dir):
            p.analyze(estimator=cand)
        _srv._invalidate_renderer()
        _drop_render(t["ara_id"])
        _mt._schedule(s)
        return True
    log.get().warning("修飾 %s: 編集の対象のノート %s が、どの方式の解析にも揃わない（今の方式 %s のまま）",
                      t["ara_id"], ", ".join(missing[:5]), cur)
    return False


def _missing_error(t, p):
    """ノートの ID に頼る編集の対象が今の解析に無い（方式探しでも揃わない・利用者が明示した方式）: 黙って
    別のノートや無しで鳴らさず、どの編集が当たらないかを ProjectError で返す（再合成しない）。"""
    lost = p._missing_note_targets()
    if not lost:
        return
    ids = sorted({nid for _e, nid in lost})
    raise ProjectError("修飾 %s: ノートの ID を対象にした編集 %d 件（%s）の対象のノート %s が、今の F0 の方式 %s の解析に無い"
                       "（missing_note_targets）。方式を補正を作った方式に戻すか、その編集を外す"
                       % (t.get("ara_id"), len(lost), ", ".join(e.id for e, _n in lost[:5]),
                          ", ".join(ids[:5]), p.f0_estimator()))


# ---------------------------------------------------------------- 版
def _rev_parts(p):
    """(解析の署名, 編集の署名)。素材・解析（テイクの F0 の方式・時刻・キャッシュのファイル）と、編集リスト・歌詞から決まる。

    `analyzed_at` は秒の単位なので、同じ秒の解析し直しはキャッシュのファイルの署名（大きさ・更新時刻）で見分ける。"""
    tk = p.take or {}
    a = (p.analysis or {}).get("take") or {}
    cache = None
    if a.get("cache"):
        try:
            st = os.stat(a["cache"])
            cache = [st.st_size, st.st_mtime_ns]
        except OSError:
            pass
    asig = hashlib.sha1(json.dumps([tk.get("sha256"), tk.get("offset_frames"), tk.get("frames"),
                                    a.get("analyzed_at"), a.get("estimator"), cache],
                                   sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]
    erev = hashlib.sha1(json.dumps([[e.to_json() for e in p.edits], p.lyrics],
                                   sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:10]
    return asig, erev


def _disk_rev(s, t):
    pdir = s.project_dir_of(t)
    if not os.path.exists(os.path.join(pdir, "project.json")):
        return EMPTY_REV
    return "%s:%s" % _rev_parts(Project(pdir).load())


# ---------------------------------------------------------------- 差分の再合成
def _merge(spans):
    from .render.region import _merge as merge
    return merge(spans)


def _inside(w, spans):
    """窓 w が spans のどれかと**中で**重なるか（端が触れるだけは重ならない）。"""
    return any(w[0] < b - 1e-9 and a < w[1] - 1e-9 for a, b in spans)


def _analysis_ready(p):
    """テイクの解析をメモリかキャッシュから読むだけで使えるか（`Project._ensure_analyzed` が計算しないで済むか）。"""
    return p._take_f0 is not None or os.path.exists(os.path.join(p.dir, "cache", "take-analysis.json"))


def _renderer_for(ara_id, p, backend, channels, asig):
    from .render.region import RegionRenderer
    key = (_norm(p.dir), (p.take or {}).get("sha256"), backend, channels, asig)
    hit = _renderers.get(ara_id)
    if hit is not None and hit[0] == key:
        _renderers.move_to_end(ara_id)
        return hit[1]
    rr = RegionRenderer.for_project(p, backend=backend, channels=channels)
    _renderers[ara_id] = (key, rr)
    _renderers.move_to_end(ara_id)
    while len(_renderers) > RENDERERS_MAX:
        _renderers.popitem(last=False)
    return rr


def audition_renderer(ara_id, p, backend, asig):
    """同じ素材・解析版ならARA再生のレンダラを試聴へ貸す（新たな全域準備を避ける）。"""
    hit = _renderers.get(ara_id)
    if hit is None:
        return None
    key, rr = hit
    if key[0] != _norm(p.dir) or key[1] != (p.take or {}).get("sha256"):
        return None
    if key[2] != backend or key[4] != asig:
        return None
    _renderers.move_to_end(ara_id)
    return rr


def audition_pcm(ara_id, p, backend, asig, rev, ia, ib):
    """ARAへ渡した同じ版のPCMに要求範囲が丸ごとあれば、その部分を返す。"""
    st = _render.get(ara_id)
    if (st is None or st.get("rev") != rev or st.get("asig") != asig
            or st.get("backend") != backend or st.get("pending")
            or st.get("project_dir") != _norm(p.dir)):
        return None
    pcm = st.get("audition_pcm")
    if (pcm is None or pcm["sr"] != int(p.take["sr"])
            or pcm["source_frames"] != int(p.take["frames"])):
        return None
    channels = pcm["channels"]
    for w in pcm["windows"]:
        first = w["start_frame"]
        last = first + w["frames"]
        if not (first <= ia < ib <= last):
            continue
        offset = w["byte_offset"] + (ia - first) * channels * 4
        length = (ib - ia) * channels * 4
        try:
            with open(pcm["path"], "rb") as f:
                f.seek(offset)
                raw = f.read(length)
            if len(raw) != length:
                return None
        except OSError:
            return None
        y = np.frombuffer(raw, dtype="<f4").reshape(-1, channels)
        if channels > 1:
            return y.mean(axis=1, dtype="float64"), (first, last)
        return y[:, 0].copy(), (first, last)
    return None


def _out_path(wd, ara_id):
    d = os.path.join(wd, OUT_DIR)
    os.makedirs(d, exist_ok=True)
    _out_seq[0] += 1
    k = _key(ara_id)
    path = os.path.join(d, "%s-%06d.f32" % (k, _out_seq[0]))
    try:
        old = sorted((f for f in os.listdir(d) if f.startswith(k + "-") and f.endswith(".f32")),
                     key=lambda f: os.path.getmtime(os.path.join(d, f)))
        for f in old[:max(0, len(old) - (KEEP_OUT - 1))]:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
    except OSError:
        pass
    return path


# ---------------------------------------------------------------- ツール
@_tool
def ara_open(work_key: str, name: str | None = None) -> dict:
    """**DAW（ARA）のプラグイン向け**: DAW のドキュメントの作業場所（`work/ara/<work_key>/`）にセッションを開く
    （無ければ作る）。文書の種類は `ara`（未保存にならない・`.gliss` を作らない・`close_project` で消さない）。

    work_key: DAW のドキュメントごとの鍵（UUID など。英数字・-・_ の 1〜64 字。ARA のアーカイブに入れて持ち回す）。
      同じ鍵なら前の作業場所（解析のキャッシュ・修飾のトラック）をそのまま使う。
    name: DAW のドキュメント名（表示用）。
    既に同じ鍵で開いていれば何もしない（opened = false）。別の鍵なら開いている曲を閉じて開く。
    """
    wd = D.ara_work_dir(work_key)
    doc = _doc()
    if doc is not None and _norm(doc.work_dir) == _norm(wd) and _srv._state.get("session") is not None:
        if name:
            doc.name = str(name)
        s = _session()
        return _ok(dir=wd, opened=False, created=False, document=doc.info(), session=_mt.summary(s))
    created = not Session.exists(wd)
    os.makedirs(wd, exist_ok=True)
    s = None
    if not created:
        try:
            s = Session.load(wd)
        except SessionError:
            raise                                # 新しい版で作られた: 上書きしない
        except Exception as e:                   # noqa: BLE001  壊れていたら退避して作り直す
            from .project.session import SESSION_FILE, _backup_broken
            _backup_broken(os.path.join(wd, SESSION_FILE), e)
            s, created = None, True
    if s is None:
        s = Session(wd)
        s.history = []
    if not s.ara:
        s.ara = True
    for t in s.tracks:
        # 前の作業場所: プラグインがソースの WAV を書き直していたら、開く前にテイクの参照を直す（編集を捨てない）
        if t.get("ara_id") and os.path.exists(t["path"]) and _sig(t["path"]) != t.get("ara_file_sig"):
            try:
                _refresh_source(s, t, t["path"], None)
            except Exception as e:               # noqa: BLE001  読めなければ ara_set_modification に任せる
                log.get().warning("修飾 %s のソースを確かめられない: %s", t.get("ara_id"), e)
    s.save()
    marker = D.read_marker(wd) or {}
    title = str(name) if name else (marker.get("name") or "DAW のドキュメント")
    D.write_marker(wd, kind="ara", name=title, work_key=str(work_key))
    _reset_render()
    log.set_log_file(os.path.join(wd, "engine.log"))
    doc = D.Document("ara", wd, name=title)
    try:
        p, why = _md._adopt_session(s, doc)
    except SessionError as e:                    # 前に選んでいたトラックが開けない: 編集対象なしで開く
        log.get().warning("DAW のドキュメントのトラックを開けない: %s", e)
        _md._clear()
        _md.set_current(doc)
        _srv._state.update(session=s, track=None, project=None)
        p, why = None, str(e)
    log.get().info("DAW のドキュメントを開いた: %s（%s・トラック %d）", wd, "新規" if created else "前の作業場所",
                   len(s.tracks))
    from . import ara_relay
    try:
        ara_relay.start(work_key, title, wd)        # 外部の AI の中継（ara_relay.py。プラグインのエンジンだけ）
    except Exception as e:                       # noqa: BLE001  中継が開けなくても文書は開く
        log.get().warning("外部の AI の中継を開けない: %s", e)
    return _ok(dir=wd, opened=True, created=created, document=doc.info(), session=_mt.summary(s),
               project_dir=p.dir if p else None, analyzed=_mt.analyzed(s, p) if p else False,
               guide_note=why)


@_tool
def ara_set_modification(ara_id: str, source_path: str, source_id: str | None = None,
                         name: str | None = None, offset_sec: float | None = None, group: str | None = None,
                         clone_of: str | None = None) -> dict:
    """**DAW（ARA）のプラグイン向け**: AudioModification のトラックを作る／直す（取り消しの履歴に入れない）。

    ara_id: AudioModification の persistentID。source_path: プラグインが書いたソースの WAV（ソースの周波数・
      チャンネルのまま。修飾の範囲はソース全体）。source_id: AudioSource の persistentID。name: 表示名。
    offset_sec: タイムラインの位置（代表のリージョンでソースの 0 秒が置かれるソングの秒。新しいトラックの既定は 0、
      既存のトラックで省けば今のまま）。group: DAW のトラック名（省けば今のまま）。
    clone_of: 複製元の ara_id（新しく作るときだけ、その編集を写す。素材の中身が違えば写さない）。
    同じファイルの別の修飾も別のトラックになる。外した ara_id を足し直すと、前の id・前の編集のまま戻る。
    既存のトラックで素材のファイルが変わっても、音が同じなら編集はそのまま。音が変わったら source_changed = true
    （編集は残し、解析はやり直す）。編集対象が無ければこのトラックを編集対象にする（selected）。
    """
    if not ara_id:
        raise SessionError("ara_id が要る")
    s = _session()
    path = os.path.abspath(str(source_path))
    if not os.path.exists(path):
        raise SessionError("ソースの音声が見つからない: %s" % path)
    t = s.find_ara(ara_id)
    created = t is None
    changed_file = source_changed = cloned = False
    if created:
        gone = s.ara_gone.pop(ara_id, None) or {}
        t = s.add_track(path, kind="vocal", name=(str(name).strip() or None) if name else None,
                        offset_sec=float(offset_sec or 0.0), project_dir="tracks/ara-%s" % _key(ara_id),
                        source_id=source_id or None, ara_id=ara_id, track_id=gone.get("id"))
        t["group"] = group
        _restore_guides(s, t, gone)
        h = _audio_sha(path)
        t.update(ara_file_sig=_sig(path), ara_audio_sha=h)
        pj = os.path.join(s.project_dir_of(t), "project.json")
        if os.path.exists(pj):
            # 外した修飾を足し直した（DAW の取り消し）・前の作業場所: 前の編集をこの素材に合わせる
            source_changed = bool(gone.get("audio_sha")) and gone["audio_sha"] != h
            _repoint(s, t, source_changed)
        elif clone_of:
            src = s.find_ara(clone_of)
            if src is not None:
                arc = _archive_of(src, s.project_dir_of(src))
                if arc is not None and _mismatch(t, arc) is None:
                    _restore_into(s, t, arc)
                    cloned = True
                    set_guide_id(t, src.get("guide_id"))     # 複製は複製元のガイドも引き継ぐ
                elif arc is not None:
                    log.get().warning("複製元 %s と素材が違うので編集を写さない: %s", clone_of, ara_id)
    else:
        changed_file, source_changed = _refresh_source(s, t, path, source_id)
        if name is not None and str(name).strip():
            t["name"] = str(name).strip()
        if offset_sec is not None:
            v = float(offset_sec)
            if not np.isfinite(v):
                raise SessionError("offset_sec が数ではない")
            if abs(v - float(t["offset_sec"])) * int(t["sr"]) >= 0.5:
                t["offset_sec"] = round(v, 6)
        if group is not None:
            t["group"] = group
        t["kind"] = "vocal"
    s.save()
    selected = False
    cur = _mt.current_track_id()
    if cur is None or _srv._state.get("project") is None:
        _mt._open_track(s, t)                    # 最初のボーカル: 編集対象にする
        selected = True
    elif cur == t["id"] and (changed_file or created):
        _mt._open_track(s, t)                    # 編集対象の素材が変わった: 開き直す
        selected = True
    else:
        selected = _mt._reopen_if_stale(s)       # ガイドの位置が変わった
    _mt._schedule(s)
    return _ok(track=_row(s, t), created=created, source_changed=source_changed, cloned=cloned,
               selected=bool(selected), analyzed=_analyzed(s, t), rev=_disk_rev(s, t), session=_mt.summary(s))


@_tool
def ara_remove_modification(ara_id: str) -> dict:
    """**DAW（ARA）のプラグイン向け**: AudioModification のトラックを外す（取り消しの履歴に入れない。
    ボーカルが 0 本になってもよい）。プロジェクトのディレクトリ（編集）は残す（DAW の取り消しで戻ってきたら、
    `ara_set_modification` で同じ ara_id を足せば同じ id・同じ編集）。無ければ removed = false。"""
    s = _session()
    t = s.find_ara(ara_id)
    if t is None:
        return _ok(removed=False, session=_mt.summary(s))
    tid = t["id"]
    was_cur = tid == _mt.current_track_id()
    _forget_history(s, tid)
    s.ara_gone[ara_id] = dict({"id": tid, "audio_sha": t.get("ara_audio_sha")}, **_guide_records(s, t))
    s.remove_track(tid)
    s.save()
    switched = None
    if was_cur:
        nxt = next((x for x in s.vocal_tracks() if os.path.exists(x["path"])), None)
        if nxt is not None:
            _mt._open_track(s, nxt)
            switched = nxt["id"]
        else:
            _close_project()
    reopened = switched is not None or (not was_cur and _mt._reopen_if_stale(s))
    _drop_render(ara_id)
    _mt._schedule(s)
    return _ok(removed=True, track=tid, switched_to=switched, reopened=bool(reopened), session=_mt.summary(s))


def _plan_guides(s, guides, unknown):
    """`ara_sync(guides=)` の検査: ([(トラック, 当てるガイドのトラック id | None)], rejected)。
    知らない ara_id は unknown に、当てられない組（伴奏・自分自身）は rejected に積んで飛ばす（ほかの組は当てる）。"""
    if guides is None:
        return [], []
    if not isinstance(guides, dict):
        raise SessionError("guides は {ara_id: ガイドの ara_id}")
    plan, rejected = [], []
    for aid, gid in guides.items():
        t = s.find_ara(aid)
        if t is None:
            unknown.append(aid)
            continue
        want = None
        if gid:
            gt = s.find_ara(gid)
            if gt is None:
                unknown.append(gid)
                continue
            why = ("伴奏のトラックはガイドにできない" if gt["kind"] != "vocal" else
                   "トラック自身はガイドにできない" if gt["id"] == t["id"] else
                   "伴奏のトラックにはガイドを指定できない" if t["kind"] != "vocal" else None)
            if why:
                rejected.append({"ara_id": aid, "guide": gid, "reason": why})
                continue
            want = gt["id"]
        plan.append((t, want))
    return plan, rejected


@_tool
def ara_sync(tracks: list | None = None, tempo: dict | None = None, guide: str | None = None,
             guides: dict | None = None) -> dict:
    """**DAW（ARA）のプラグイン向け**: DAW の位置・名前・テンポをまとめて当てる（取り消しの履歴に入れない）。

    tracks: [{ara_id, offset_sec?, name?, group?}]（変わったものだけでよい）。位置は 1 サンプル未満の差なら変えない。
    tempo: {bpm, numerator, denominator, start_sec}（MusicalContext。session.tempo に source = "daw" で書く）。
    guide: ガイドにする修飾の ara_id（"" で外す。省けば今のまま）。アーカイブの document.guide を戻すとき用
      （画面のガイドの指定 set_guide_track と違い、取り消しの履歴に入れない）。
    guides: トラックごとのガイド {修飾の ara_id: ガイドの修飾の ara_id}（"" か null でそのトラックの指定を外す）。
      アーカイブの document.guides を戻すとき用（画面の set_track_guide と違い、取り消しの履歴に入れない）。
      渡した修飾だけを変える（省いた修飾はそのまま）。知らない ara_id は unknown に、当てられない組（伴奏・自分自身）は
      rejected（[{ara_id, guide, reason}]）に返して、その組だけ飛ばす（ほかの組と tracks・tempo は当てる）。
    編集対象かガイドの位置が変わって、編集対象のガイドの重ね方が変わったら開き直す（reopened = true →
    画面は analyze_take から描き直す）。知らない ara_id は unknown に返す。
    """
    s = _session()
    changed, unknown = [], []
    guide_plan, rejected = _plan_guides(s, guides, unknown)   # 位置などを変える前に検査する（不正な組は rejected に積んで飛ばす）
    for e in tracks or []:
        if not isinstance(e, dict):
            raise SessionError("tracks の要素は {ara_id, offset_sec?, name?, group?}")
        t = s.find_ara(e.get("ara_id"))
        if t is None:
            unknown.append(e.get("ara_id"))
            continue
        did = False
        if e.get("offset_sec") is not None:
            v = float(e["offset_sec"])
            if not np.isfinite(v):
                raise SessionError("offset_sec が数ではない: %s" % e.get("ara_id"))
            if abs(v - float(t["offset_sec"])) * int(t["sr"]) >= 0.5:
                t["offset_sec"] = round(v, 6)
                did = True
        if e.get("name") is not None and str(e["name"]).strip() and str(e["name"]).strip() != t["name"]:
            t["name"] = str(e["name"]).strip()
            did = True
        if "group" in e and e["group"] != t.get("group"):
            t["group"] = e["group"]
            did = True
        if did:
            changed.append(t["id"])
    tempo_changed = False
    if tempo is not None:
        nt = norm_tempo({"bpm": tempo.get("bpm"), "num": tempo.get("numerator", tempo.get("num")),
                         "den": tempo.get("denominator", tempo.get("den")),
                         "start_sec": tempo.get("start_sec"), "source": "daw"})
        if nt is None:
            raise SessionError("tempo が読めない・範囲の外（bpm 20〜400・拍子 1〜16 / 2・4・8・16）: %r" % (tempo,))
        if nt != s.tempo:
            s.tempo = nt
            tempo_changed = True
    guide_changed = False
    if guide is not None:
        g = None
        if guide:
            gt = s.find_ara(guide)
            if gt is None:
                unknown.append(guide)
            elif gt["kind"] != "vocal":
                raise SessionError("伴奏のトラックはガイドにできない: %s" % guide)
            else:
                g = gt["id"]
        if (g is not None or not guide) and g != s.guide:
            s.guide = g
            guide_changed = True
    guides_changed = []
    for t, want in guide_plan:
        if t.get("guide_id") != want:
            set_guide_id(t, want)
            guides_changed.append(t["id"])
    guide_changed = guide_changed or bool(guides_changed)
    if changed or tempo_changed or guide_changed:
        s.save()
    reopened = _mt._reopen_if_stale(s)
    if changed or guide_changed:
        _mt._schedule(s)
    return _ok(changed=changed, unknown=unknown, tempo=copy.deepcopy(s.tempo), tempo_changed=tempo_changed,
               guide=s.guide, guide_changed=guide_changed, guides=_guides_of(s),
               guides_changed=guides_changed, rejected=rejected, reopened=bool(reopened), session=_mt.summary(s))


@_tool
def ara_render_dirty(ara_id: str, since: str | None = None, backend: str = "praat", channels: str = "all",
                     max_sec: float | None = None) -> dict:
    """**DAW（ARA）のプラグイン向け**: 修飾の編集を当てた音の**差分**（前に渡した版 `since` から）。

    返り値: rev（今の版。途中なら `<版>~<n>`）・reset（true なら手元の窓を全部捨てて原音から当て直す。
    初回・since が違う・エンジンを起動し直した・解析や素材が変わった・backend / channels が違う）・
    restore（[[start_frame, frames]]。原音に戻す範囲）・windows（[{start_frame, frames, byte_offset}]。
    再合成した窓。PCM は path の .f32 = float32 リトルエンディアンのインターリーブを窓の順に並べたもの）・
    more（max_sec を超えた残りがある。since=rev で続けて呼ぶ）・sr・channels・source_frames・
    analysis_pending（編集はあるがテイクの解析がまだ。窓は空＝原音のまま。解析が済むと ara_revs の版が変わる）。
    当てる順: reset なら空から → restore の範囲を原音に → windows を置く（重なる古い窓は消す）。
    結果は render_region の全体（＝書き出し）とサンプル単位で同じ。編集の無い修飾は空（原音のまま）。
    backend: "praat"（既定）/ "psola"。channels: "all"（既定。ソースのチャンネルのまま）/ "mono"。
    max_sec: 1 回で再合成する窓の長さの上限（秒。既定 10。少なくとも窓 1 つは返す）。
    """
    from .project.pitch import layered_segments
    from .render.base import resolve_backend_name
    from .render.region import dirty_windows, windows_for
    t_all = time.perf_counter()
    if channels not in ("all", "mono"):
        raise ProjectError("channels は all か mono")
    s = _session()
    t = _track(s, ara_id)
    name = resolve_backend_name(backend)
    limit = DEFAULT_MAX_SEC if max_sec is None else max(0.0, float(max_sec))
    p, _is_cur = _mt._track_project(s, t)
    rr = None
    # 編集はあるがテイクの解析がまだ（別の PC でアーカイブから戻した直後など）: ロックを握って解析を待たず、
    # 原音のまま返す（analysis_pending）。解析が済めば版の解析の署名が変わるので、プラグインは呼び直す
    pending_analysis = p is not None and bool(p.edits) and not _analysis_ready(p)
    if p is None:
        rev, asig, segs, wins = EMPTY_REV, None, [], []
        sr, n = int(t["sr"]), int(t["source_frames"])
        ch = int(t["channels"]) if channels == "all" else 1
    else:
        if p.edits and not pending_analysis:
            p.ensure_analyzed()
            _fit_estimator(s, t, p)              # 編集の対象のノートが無いとき、当たる方式に替える（アーカイブの方式が合わない曲）
            _missing_error(t, p)
        asig, erev = _rev_parts(p)
        rev = "%s:%s" % (asig, erev)
        if p.edits and not pending_analysis:
            rr = _renderer_for(ara_id, p, name, channels, asig)
            segs = layered_segments(p)
            wins = windows_for(p, segs)
            sr, n, ch = rr.sr, rr.n_frames, rr.n_ch
        else:
            segs, wins = [], []                  # 編集が無い・解析待ち: 窓も無い（原音のまま。解析を待たない）
            sr, n = int(p.take["sr"]), int(p.take["frames"])
            ch = int(p.take["channels"]) if channels == "all" else 1
    st = _render.get(ara_id)
    reset = (st is None or since is None or since != st["rev"] or st["backend"] != name
             or st["channels"] != channels or st["asig"] != asig)
    if reset:
        spans = [list(w) for w in wins]
        restore_spans = []
    else:
        dirty = []
        if st["segs"] or segs:
            dirty = dirty_windows(p, st["segs"], segs, st["windows"], wins)
        spans = _merge(dirty + st["pending"])
        restore_spans = spans
    todo = [w for w in wins if _inside(w, spans)]
    restore = []
    for a, b in restore_spans:
        da, db = max(0, int(round(a * sr))), min(n, int(round(b * sr)))
        if db > da:
            restore.append([da, db - da])
    done, out_windows, chunks, total = [], [], [], 0.0
    prep_sec = rr.prepare_sec if rr is not None else 0.0
    t_render = time.perf_counter()
    if todo:
        with _srv._prep_yield():
            byte = 0
            for w in todo:
                wa, wb = max(0, int(round(w[0] * sr))), min(n, int(round(w[1] * sr)))
                if done and total + (w[1] - w[0]) > limit:
                    break
                done.append(w)
                total += w[1] - w[0]
                if wb - wa < 2:
                    continue
                yw, _meta = rr.render_frames(wa, wb, segs)
                buf = np.ascontiguousarray(yw, dtype="<f4")
                chunks.append(buf)
                out_windows.append({"start_frame": int(wa), "frames": int(wb - wa), "byte_offset": int(byte)})
                byte += buf.nbytes
    prep_sec = (rr.prepare_sec if rr is not None else 0.0) - prep_sec    # 下ごしらえ（初回だけ。窓の再合成の中で作る）
    render_sec = time.perf_counter() - t_render - prep_sec
    path = None
    if chunks:
        path = _out_path(s.dir, ara_id)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            for c in chunks:
                f.write(c.tobytes())
        os.replace(tmp, path)
    remaining = todo[len(done):]
    token = rev
    if remaining:
        _out_seq[0] += 1
        token = "%s~%d" % (rev, _out_seq[0])
    previous_pcm = (st.get("audition_pcm") if st is not None and not reset
                    and st.get("rev") == rev else None)
    current_pcm = ({"path": path, "windows": out_windows, "channels": int(ch),
                    "sr": int(sr), "source_frames": int(n)} if path else previous_pcm)
    _render[ara_id] = {"rev": token, "segs": segs, "windows": [list(w) for w in wins],
                       "pending": _merge([list(w) for w in remaining]), "backend": name,
                       "channels": channels, "asig": asig,
                       "project_dir": _norm(p.dir) if p is not None else None,
                       "audition_pcm": current_pcm}
    return _ok(track=t["id"], ara_id=ara_id, rev=token, reset=reset, more=bool(remaining),
               analysis_pending=pending_analysis, sr=int(sr),
               channels=int(ch), source_frames=int(n), restore=restore, windows=out_windows, path=path,
               rendered_sec=round(total, 4), backend=rr.actual_backend if rr is not None else name,
               timing_sec={"prepare": round(prep_sec, 4),
                           "render": round(render_sec, 4),
                           "total": round(time.perf_counter() - t_all, 4)})


@_tool(lock=False)
def ara_revs() -> dict:
    """**DAW（ARA）のプラグイン向け**: 全修飾の今の版 `{revs: {ara_id: rev}, track_ids: {ara_id: track_id}}`。
    エンジンのロックを取らずにディスクの project.json から読む（解析のジョブの最中でもすぐ返る）。
    プラグインは手元の版と違う修飾だけ ara_render_dirty を呼ぶ（取り消しで別のトラックが変わったときも拾える）。
    プロジェクトがまだ無い修飾は "empty"。
    external: 外部の AI の中継（ara_relay.py）が開いていれば `{seq, session_seq, track_id}`。外部の AI が曲を変えるたびに
    seq が進み（セッションを変えうるものは session_seq も）、プラグインは画面に project-changed（・session-changed）を知らせる。"""
    s = _srv._state.get("session")
    if _doc() is None or s is None:
        raise ProjectError("DAW のドキュメントが開かれていない（先に ara_open を呼ぶ）")
    revs, ids, errors = {}, {}, {}
    for t in list(s.tracks):
        aid = t.get("ara_id")
        if not aid:
            continue
        ids[aid] = t["id"]
        try:
            revs[aid] = _disk_rev(s, t)
        except Exception as e:                   # noqa: BLE001  書きかけ・壊れている: その修飾だけ
            revs[aid] = None
            errors[aid] = str(e)
    from . import ara_relay
    return _ok(revs=revs, track_ids=ids, errors=errors or None, external=ara_relay.external())


@_tool(lock=False)
def ara_archive(ara_ids: list | None = None) -> dict:
    """**DAW（ARA）のプラグイン向け**: 保存（ARA のアーカイブ）に入れる各修飾の編集の状態
    `{archives: {ara_id: {name, track, archive}}, guide: <共通のガイドの ara_id | null>,
    guides: {ara_id: トラックごとのガイドの ara_id}, tempo}`。

    archive は `Project.to_archive()` と同じ形（素材の参照・歌詞・編集の changeset の列。解析・画面の状態は入らない。
    ガイドは入れない）。エンジンのロックを取らずにディスクの project.json から作る（解析のジョブの最中でも
    保存を止めない）。プロジェクトがまだ無い修飾（編集なし）は archive = null。ara_ids を省くと全部。"""
    s = _srv._state.get("session")
    if _doc() is None or s is None:
        raise ProjectError("DAW のドキュメントが開かれていない（先に ara_open を呼ぶ）")
    want = set(ara_ids) if ara_ids else None
    out, errors = {}, {}
    for t in list(s.tracks):
        aid = t.get("ara_id")
        if not aid or (want is not None and aid not in want):
            continue
        try:
            out[aid] = {"name": t["name"], "track": t["id"], "archive": _archive_of(t, s.project_dir_of(t))}
        except Exception as e:                   # noqa: BLE001  その修飾だけ
            errors[aid] = str(e)
    g = None
    if s.guide:
        g = next((t.get("ara_id") for t in list(s.tracks) if t["id"] == s.guide), None)
    missing = sorted(want - set(out) - set(errors)) if want is not None else []
    return _ok(archives=out, guide=g, guides=_guides_of(s), tempo=copy.deepcopy(s.tempo),
               errors=errors or None, missing=missing or None)


@_tool
def ara_restore(ara_id: str, archive: dict) -> dict:
    """**DAW（ARA）のプラグイン向け**: ARA のアーカイブから修飾の編集を戻す（取り消しの履歴に入れない。
    戻した編集も Ctrl+Z の列には入らない）。

    archive: ara_archive の archives[ara_id].archive（`Project.to_archive()` の形）。
    素材（長さ・音の中身）がトラックの今の素材と違えば**戻さずに** mismatch = true と reason を返す（ok は true。
    プラグインはアーカイブを捨てずに持ち続ける）。編集対象のトラックなら開き直す（reopened）。
    archive の `f0_estimator`（補正を作った F0 の方式）があれば、そのトラックの方式にする（estimator_applied。
    別の PC・別の作業場所で開き直しても同じ方式で解析する）。この PC で使えない方式（重みが無い）は当てず、
    estimator_note に理由を返す。"""
    s = _session()
    t = _track(s, ara_id)
    why = _mismatch(t, archive)
    if why:
        log.get().warning("修飾 %s のアーカイブを当てない: %s", ara_id, why)
        return _ok(track=t["id"], ara_id=ara_id, mismatch=True, reason=why, edits=None, changesets=None,
                   reopened=False)
    applied, note = _apply_estimator(s, t, archive.get("f0_estimator"))
    if note:
        log.get().warning("修飾 %s の F0 の方式を当てない: %s", ara_id, note)
    p = _restore_into(s, t, archive)
    s.save()
    reopened = False
    if t["id"] == _mt.current_track_id():
        _mt._open_track(s, t)
        reopened = True
    _mt._schedule(s)
    return _ok(track=t["id"], ara_id=ara_id, mismatch=False, edits=len(p.edits), changesets=len(p.changesets),
               reopened=reopened, estimator_applied=applied, estimator_note=note, rev=_disk_rev(s, t),
               session=_mt.summary(s))


def _note_row(n, start, end, midi):
    hz = 440.0 * 2.0 ** ((midi - 69.0) / 12.0)
    db = n.rms_peak_db if n.rms_peak_db is not None and np.isfinite(n.rms_peak_db) else NOTE_FLOOR_DB / 2
    vol = min(1.0, max(0.0, (float(db) - NOTE_FLOOR_DB) / -NOTE_FLOOR_DB))
    return {"id": n.id, "start_sec": round(float(start), 6), "end_sec": round(float(end), 6),
            "hz": round(float(hz), 4), "midi": round(float(midi), 4), "volume": round(vol, 4)}


def _notes_of(p):
    """(編集を当てた後のノート, 解析だけのノート)。どちらもソースの秒で頭の順・音程のあるノートだけ。

    編集後: 分割・結合を当て、位置は時間の写像（画面の `edited_start_sec` / `edited_end_sec` と同じ）、
    音程はノートの中心（画面の `edited_pitch_midi` と同じ。つなぎ・鉛筆を除く基本の段）。無音にしたノートは除く。"""
    from .project.pitch import pitch_model
    from .view.export_data import _center, _map_time, build_time_map, cents_offset
    p.ensure_analyzed()
    f0r = p.take_f0
    hop = f0r.hop_s
    base = [n for n in (p._take_notes or []) if n.kind == "note" and n.pitch_midi is not None]
    source = [_note_row(n, n.start_sec, n.end_sec, float(n.pitch_midi)) for n in base]
    if not p.edits:
        return [dict(r) for r in source], source
    base_segs, _segs, _lay, _trs = pitch_model(p)
    src_pts, out_pts = build_time_map(base_segs, 0.0, p.duration_sec)
    off = cents_offset(base_segs, f0r.times)
    mutes = [(float(e.target.start_sec), float(e.target.end_sec)) for e in p.edits if e.kind == "mute"]
    edited = []
    for n in p.take_notes:
        if n.kind != "note" or n.pitch_midi is None:
            continue
        ln = n.end_sec - n.start_sec
        cov = sum(max(0.0, min(b, n.end_sec) - max(a, n.start_sec)) for a, b in mutes)
        if ln > 0 and cov >= 0.5 * ln:
            continue
        es = float(_map_time(src_pts, out_pts, n.start_sec))
        ee = float(_map_time(src_pts, out_pts, n.end_sec, "left"))
        if ee <= es:
            continue
        edited.append(_note_row(n, es, ee, _center(n, off, hop)))
    edited.sort(key=lambda r: (r["start_sec"], r["end_sec"]))
    return edited, source


@_tool
def ara_notes(ara_ids: list | None = None) -> dict:
    """**DAW（ARA）のプラグイン向け**: DAW に返すノート（ARA の content reader の kARAContentTypeNotes）。

    返り値: `notes: {ara_id: {track, rev, state, edited, notes, source_notes}}`。
    state: "ready"（解析が済んだ）/ "pending"（解析がまだ。裏の準備が済むと ara_revs の版が変わる）/
    "empty"（プロジェクトがまだ無い）。ready のときだけ notes・source_notes が入る（それ以外は空の配列）。
    notes = 編集を当てた後、source_notes = 解析だけ（どちらもソースの秒。`[{id, start_sec, end_sec, hz, midi, volume}]`、
    頭の順、音程のあるノートだけ。notes からは無音にしたノートを除く）。hz は中心の音程の Hz、volume は
    ノートの音量の山（-60 dB → 0、0 dB → 1）。edited = 編集リストが空でない（DAW には adjusted と出す）。
    rev は ara_revs と同じ版（プラグインは版が変わった修飾だけ取り直す）。解析は待たない・始めない
    （編集対象でないトラックもディスクのプロジェクトから読む。編集対象は変えない）。ara_ids を省くと全部。"""
    s = _session()
    want = set(ara_ids) if ara_ids else None
    out, errors = {}, {}
    for t in list(s.tracks):
        aid = t.get("ara_id")
        if not aid or (want is not None and aid not in want):
            continue
        try:
            p, _is_cur = _mt._track_project(s, t)
            row = {"track": t["id"], "rev": EMPTY_REV, "state": "empty", "edited": False,
                   "notes": [], "source_notes": []}
            if p is not None:
                row["rev"] = "%s:%s" % _rev_parts(p)
                row["edited"] = bool(p.edits)
                if _analysis_ready(p):
                    row["notes"], row["source_notes"] = _notes_of(p)
                    row["state"] = "ready"
                else:
                    row["state"] = "pending"
            out[aid] = row
        except Exception as e:                   # noqa: BLE001  その修飾だけ
            errors[aid] = str(e)
    missing = sorted(want - set(out) - set(errors)) if want is not None else []
    return _ok(notes=out, errors=errors or None, missing=missing or None)


# ---------------------------------------------------------------- 単体の .gliss の編集を DAW の文書へ移す
@_tool(lock=False)
def export_edits(gliss_path: str, track: str | None = None, estimator: str | None = None,
                 include_archive: bool = True) -> dict:
    """`.gliss`（単体のプロジェクト）のトラックの編集を、ARA のアーカイブ（`Project.to_archive()` の形）にして返す（読むだけ）。

    gliss_path: `.gliss` のファイル（保存した中身を読む）。track: トラックの id か名前（省くと、ボーカルが 1 本ならそれ）。
    estimator: F0 の方式を決め打ちする（省くと、トラックに明示した方式。記録が無ければ null）。
    include_archive: false なら archive を省く（要約だけ見る。`import_edits(gliss_path=…)` はこの archive を自分で作る）。
    返り値: archive・track（id・name・gliss）・material（素材の識別: name・frames・sr・channels・sha256・
    clip_audio_sha256 = 音の中身のハッシュ。ARA 側が照らす値）・estimator（補正を作った F0 の方式 | null）・
    stats（edits・changesets・authors・kinds・note_targets = ノートの ID に頼る編集の数）・
    not_transferred（移らないセッションの項目: mutes・cuts・ミキサー・ガイドの指定）・warnings。
    クリップ（素材の一部）のトラック・音声が見つからないトラックはエラー（ARA の文書は素材の全体を 1 つの修飾にする）。
    DAW の文書を選んでいる間（ara_attach の後）も、このエンジンの中で動く（ファイルを読むのは AI のエンジン）。"""
    r = _tr.archive_from_gliss(gliss_path, track=track, estimator=estimator)
    if not include_archive:
        r.pop("archive")
    return _ok(**r)


def _import_target(s):
    """`import_edits` の宛先: 選んでいる修飾のトラック（外部の AI なら ara_attach で選んだもの）。"""
    tid = _srv._state.get("track")
    t = None
    if tid:
        try:
            t = s.track(tid)
        except SessionError:
            t = None
    if t is None or not t.get("ara_id"):
        raise ProjectError("DAW の文書の修飾が選ばれていない（外部の AI は ara_documents → ara_attach で修飾を選ぶ。"
                           "単体の曲に当てるなら load_project で .gliss を開く）")
    return t


def _json_equal(a, b):
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


@_tool
def import_edits(archive: dict | None = None, gliss_path: str | None = None, track: str | None = None,
                 replace: bool = False, estimator: str | None = None, analyze: bool = True) -> dict:
    """**DAW の Gliss の文書の、選んでいる修飾**へ、編集（ARA のアーカイブ）を当てる。単体の `.gliss` の補正を、
    DAW の中の Gliss（ARA）へ移すためのツール（外部の AI は ara_attach → import_edits → ara_detach）。

    archive: `export_edits` の archive（または `ara_archive` の形）。gliss_path・track: archive の代わりに `.gliss` の
      トラックを直接指す（`export_edits` と同じ。中で archive を作る）。どちらか 1 つ。
    素材が違えば（長さ・音の中身のハッシュ）**何も変えずに** mismatch = true と reason を返す（ok は true）。
    修飾に編集が既にあるとき、取り込む編集と違うなら replace = true が要る（画面で手を入れた編集を黙って消さない。
    replaced に消えた編集の数と author の内訳を返す）。
    estimator: F0 の方式を決め打ちする（省くと archive の `f0_estimator`）。方式があれば、その修飾の方式にして
      （裏の準備も追従する）、解析がその方式でなければ**ここで解析する**（analyze = true。数秒〜数十秒、エンジンを占有する）。
      補正を作った方式と違うと再合成の音が変わり、ノートの ID に頼る編集は当たらなくなる。方式の記録が無い archive は
      estimator を指定するか、`analyze_take(estimator=…)` で方式を決め、missing_note_targets が 0 になる方式にする。
      この PC で使えない方式（rmvpe の重みが無い）は当てず warnings に出す。
    明示的な取り込みは、置換前の補正とともに Ctrl+Z / Ctrl+Y の 1 操作にする。
    ホストの初期読込 `ara_restore` は履歴に加えない。補正内の author は archive のまま保つ。
    当てた後は外部の編集と同じ道で再合成・DAW への反映（ara_revs の版・プラグインへの通知）に乗る。
    返り値: mismatch・imported・track・ara_id・edits・changesets・authors・estimator・estimator_applied・analysis
    （estimator・ran）・missing_note_targets（{count, ids}。解析が済んでいなければ null）・replaced・
    not_transferred（gliss_path で指したときだけ）・warnings・next。"""
    s = _session()
    t = _import_target(s)
    info = None
    if (archive is None) == (gliss_path is None):
        raise ProjectError("archive か gliss_path のどちらか 1 つを渡す")
    if gliss_path is not None:
        info = _tr.archive_from_gliss(gliss_path, track=track, estimator=estimator)
        archive = info["archive"]
    elif estimator:
        archive = dict(archive, f0_estimator=estimator)
    why = _mismatch(t, archive)
    if why:
        log.get().warning("修飾 %s に編集を取り込まない: %s", t.get("ara_id"), why)
        return _ok(track=t["id"], ara_id=t["ara_id"], mismatch=True, imported=False, reason=why)
    pdir = s.project_dir_of(t)
    replaced = None
    before_estimator = t.get("estimator")
    s.ensure_history()
    mark_before = int(s.history_marks.get(t["id"]) or 0)
    if os.path.exists(os.path.join(pdir, "project.json")):
        q = Project(pdir).load()
        if q.changesets:
            same = _json_equal([c.to_json() for c in q.changesets], archive.get("changesets") or [])
            if not same and not replace:
                st = _tr.edit_stats(q.to_archive())
                raise ProjectError("この修飾には別の編集がある（編集 %d 件・author %s）。入れ替えるなら replace=true"
                                   % (st["edits"], st["authors"]))
            if not same:
                st = _tr.edit_stats(q.to_archive())
                replaced = {"edits": st["edits"], "changesets": st["changesets"], "authors": st["authors"]}
    warnings = list((info or {}).get("warnings") or [])
    old_track = copy.deepcopy(t)
    old_history = copy.deepcopy(s.history)
    old_marks = dict(s.history_marks)
    dropped = []
    ran, analysis_error = False, None
    try:
        # 裏準備が一時キャッシュを書き終えるまで待ってから、復元前の控えを作る。
        with prep.exclusive(pdir):
            before_archive = _archive_of(t, pdir)
            with _mt._recover_projects([pdir]):
                est, note = _apply_estimator(s, t, archive.get("f0_estimator"))
                p = _restore_into(s, t, archive, forget_history=False)
                pref = s.estimator_of(t)
                p.estimator_pref = pref
                after_archive = _archive_of(t, pdir)
                if before_archive is None:
                    before_archive = copy.deepcopy(after_archive)
                    before_archive.update(changesets=[], seq={"edit": 0, "changeset": 0},
                                          lyrics={}, auto_lyrics_attempted=False,
                                          f0_estimator=before_estimator)
                if not _json_equal(before_archive, after_archive) or before_estimator != t.get("estimator"):
                    entry, dropped = s.record_archive(t["id"], before_archive, after_archive,
                                                      before_estimator, t.get("estimator"), mark_before)
                s.save()
                if analyze and est:
                    try:
                        if not p.analysis_cached(est):
                            p.analyze(estimator=est)
                            ran = True
                    except Exception as e:       # noqa: BLE001  編集は取り込んだ。解析は analyze_take で
                        analysis_error = str(e)
                        log.get().warning("取り込んだ後の解析に失敗: %s", e)
                        warnings.append("解析に失敗した（analyze_take(estimator=%r) でやり直す）: %s" % (est, e))
                _mt._schedule(s)
    except BaseException:
        t.clear()
        t.update(old_track)
        s.history = old_history
        s.history_marks = old_marks
        cur = _srv._state.get("project")
        if cur is not None and _norm(cur.dir) == _norm(pdir):
            cur.load()
            cur._forget_analysis()
        raise
    if analyze and analysis_error is None and prep.enabled():
        try:
            prepared = prep.join(s.dir, t["id"])
            if prepared and prepared.get("state") == prep.FAILED:
                raise ProjectError(prepared.get("error") or "裏の準備に失敗した")
            p.reload_if_changed()
        except Exception as e:                   # noqa: BLE001  取り込みと履歴の保存は既に完了した
            analysis_error = str(e)
            log.get().warning("取り込み後の準備が未完了: %s", e)
            warnings.append("編集は取り込んだ。準備は未完了（後で解析をやり直す）: %s" % e)
    _mt._discard_dropped(s, dropped)
    if note:
        warnings.append(note)
    cur = _srv._state.get("project")
    if cur is not None and cur is not p and _norm(cur.dir) == _norm(p.dir):
        cur.load()                               # 画面の編集対象が古い Project のまま（同じトラック）: 読み直す
        cur._forget_analysis()
        cur.estimator_pref = pref
    if ran and cur is not None and cur is not p and _norm(cur.dir) == _norm(p.dir):
        cur.load()
        cur._forget_analysis()
    _srv._invalidate_renderer()
    _drop_render(t["ara_id"])
    missing = None
    if _analysis_ready(p) and analysis_error is None and (est is None or ran or p.analysis_cached(est)):
        p.ensure_analyzed()
        ids = [nid for _e, nid in p._missing_note_targets()]
        missing = {"count": len(ids), "ids": sorted(set(ids))[:20]}
        if ids:
            warnings.append("ノートの ID に頼る編集 %d 件の対象のノートが無い（解析の方式が補正を作った方式と違う）。"
                            "estimator を合わせる" % len(ids))
    st = _tr.edit_stats(archive)
    ta = (p.analysis or {}).get("take") or {}
    return _ok(track=t["id"], ara_id=t["ara_id"], mismatch=False, imported=True, edits=len(p.edits),
               changesets=len(p.changesets), authors=st["authors"], estimator=archive.get("f0_estimator"),
               estimator_applied=est, analysis={"estimator": ta.get("estimator"), "ran": ran},
               missing_note_targets=missing, replaced=replaced,
               not_transferred=(info or {}).get("not_transferred"), warnings=warnings,
               next="list_changes・list_notes で確かめる。方式の記録が無いときは analyze_take(estimator=…) → "
                    "missing_note_targets が 0 になる方式。DAW の再生・書き出しは約 1 秒で追従する")


# ---------------------------------------------------------------- 外部の AI から DAW の文書を操作する（ara_relay.py）
def _relay_local():
    from . import bridge
    if bridge.is_app():
        raise ProjectError("画面・DAW のプラグインが起動したエンジンでは使えない（外部の AI のエンジン用）")


@_tool(lock=False)
def ara_documents() -> dict:
    """DAW（Fender Studio Pro など）の中で開いている Gliss（ARA プラグイン）の文書の一覧。

    文書ごとに document（DAW の文書名）・work_key・daw（DAW の実行ファイル）・allow（DAW の Gliss が AI に許すこと）・
    tracks（修飾ごとに track_id・ara_id（persistentID）・name（修飾の名前）・daw_track（DAW のトラック名）・
    duration_sec（ソースの長さ）・analyzed（解析済みか）・prep（裏の準備の状態）・editing_in_plugin（プラグインの画面で
    開いているか））。attached: 今 ara_attach で選んでいる修飾（無ければ null）。
    次に ara_attach(ara_id) で選ぶと、以後のツール（analyze_take・list_notes・shift_pitch…）はその修飾に効く。
    """
    from . import ara_relay
    _relay_local()
    docs = ara_relay.documents()
    return _ok(documents=docs, attached=ara_relay.selection(), sessions_dir=ara_relay.sessions_dir(),
               next=("ara_attach(ara_id) で修飾を選ぶ" if docs else
                     "DAW で Gliss を挿したイベントを開くと出る（DAW の Gliss が %s=off なら出ない）" % ara_relay.ALLOW_ENV))


@_tool(lock=False)
def ara_attach(ara_id: str = None, track_id: str = None, document: str = None) -> dict:
    """DAW の Gliss の文書の修飾（オーディオのイベントの編集の単位）を選び、以後のツールをそこへ転送する。

    ara_id / track_id: ara_documents の修飾（省くと、修飾が 1 つならそれ、ほかはプラグインの画面で開いているもの）。
    document: 文書が複数あるとき、その work_key（または文書名・engine_pid）。
    選んだ後は analyze_take（解析済みならすぐ返る）→ list_notes / list_deviations → shift_pitch などの編集を
    単体のときと同じ名前・引数で呼ぶ。編集は DAW の再生・プラグインの画面に反映され、DAW のソングに保存される。
    別の修飾へは select_track(track_id) か ara_attach をもう一度。単体の Gliss に戻るときは ara_detach。
    DAW の文書の作り・トラックの増減・保存（load_project・add_track・save_project など）は使えない。
    """
    from . import ara_relay
    _relay_local()
    d, row = ara_relay.attach(ara_id=ara_id, track_id=track_id, document=document)
    return _ok(attached=ara_relay.selection(), track=row, document=d.get("document"), work_key=d.get("work_key"),
               daw=d.get("daw"), allow=d.get("allow"),
               next="analyze_take → list_notes（以後のツールはこの修飾に効く）")


@_tool(lock=False)
def ara_detach() -> dict:
    """ara_attach をやめる（以後のツールはこのエンジン＝単体の Gliss の曲に戻る）。"""
    from . import ara_relay
    _relay_local()
    return _ok(detached=ara_relay.detach())


TOOLS = [ara_open, ara_set_modification, ara_remove_modification, ara_sync, ara_render_dirty, ara_revs,
         ara_archive, ara_restore, ara_notes, export_edits, import_edits, ara_documents, ara_attach, ara_detach]
