# -*- coding: utf-8 -*-
"""MCP サーバー（stdio）。測る／直す／確かめる を公開する。

MCP クライアント側の制約に合わせてある:
  - **結果に画像を入れない。** PNG はファイルに書いてパスを返す。
  - **生の数値列を返さない。** F0 は要約統計＋（必要なら）NPY のパス。
  - **30 秒以内に返す。** 超えそうな処理は job_id を返して `get_job` で取りに来てもらう。
  - **stderr は捨てられる。** ログは `<project>/engine.log` に自前で書く。
  - **`notifications/initialized` を送らないクライアントがある。**
    mcp 2.x のサーバーは `initialize` を受けた時点で受付を開くので、そのままで通る
    （`mcp/server/connection.py` の `initialize_accepted`）。ここでは print で stdout を
    壊さないように stdout をログへ逃がすだけにしてある。

起動: `python -m vocal_engine.mcp`
"""
import contextlib
import functools
import inspect
import os
import re
import threading
import time
import traceback
import uuid

import numpy as np

from . import __version__, bridge, log
from .analysis import f0 as f0mod
from .analysis.notes import notes_in_range
from .analysis.phonemes import get_phonemes as _get_phonemes
from .audio import write_wav
from .project import Project, ProjectError, ProjectConflict
from .project.model import Target
from .render.base import resolve_backend_name
from .render.pipeline import Renderer, segments_for
from .view import render_view as _render_view
from .view.export_data import export_view_data as _export_view_data

SERVER_NAME = "gliss"
JOB_THRESHOLD_SEC = 20.0        # これを超えそうな処理はジョブにする
_state = {"project": None, "renderer": None, "renderer_backend": None, "renderer_audio_sig": None,
          "region": {},
          "session": None, "track": None,     # session / track: トラック（mcp_tracks.py）
          "document": None}                   # 開いているプロジェクトのファイル（mcp_document.py。issue #33）
_jobs = {}
_plans = {}                     # plan_id -> timing.Plan（このプロセスの中だけ）
MAX_PLANS = 64
_lock = threading.RLock()
_jobs_lock = threading.RLock()


# ---------------------------------------------------------------- 共通
def _ok(**kw):
    return dict(ok=True, **kw)


def _project(required=True):
    p = _state["project"]
    if p is None and required:
        raise ProjectError("プロジェクトが開かれていない。先に open_project を呼ぶこと。")
    return p


def _invalidate_renderer():
    _state["renderer"] = None
    _state["renderer_audio_sig"] = None
    _state["region"] = {}


def _renderer(backend="praat"):
    # キーは実際に使うバックエンド名（praat が使えず psola に落ちたとき、"praat" と "psola" を
    # 交互に頼まれても同じレンダラを使い回し、prepare をやり直さない）
    name = resolve_backend_name(backend)
    p = _project()
    if isinstance(p, Project):
        p._check_audio_versions()
        audio_sig = (p.dir, p._audio_sigs.get("take"))
    else:
        audio_sig = None
    if (_state["renderer"] is None or _state["renderer_backend"] != name
            or _state["renderer_audio_sig"] != audio_sig):
        x, sr = p.audio("take")
        f0r = p.take_f0
        _state["renderer"] = Renderer(x, sr, f0r.f0, f0r.voiced, f0r.hop_s, backend=name)
        _state["renderer_backend"] = name
        _state["renderer_audio_sig"] = audio_sig
    return _state["renderer"]


def _range(start_sec=None, end_sec=None):
    p = _project()
    d = p.duration_sec
    t0 = 0.0 if start_sec is None else max(0.0, float(start_sec))
    t1 = d if end_sec is None else min(d, float(end_sec))
    if t1 <= t0:
        raise ProjectError("範囲が不正（start %.3f >= end %.3f）" % (t0, t1))
    return t0, t1


def _resolve_target(note_id=None, start_sec=None, end_sec=None, phoneme_id=None):
    """note_id / 範囲 / phoneme_id のどれかを Target にする。"""
    p = _project()
    if phoneme_id:
        ph = p.phoneme(phoneme_id)              # 無ければここで例外
        return Target.range(ph.start_sec, ph.end_sec, phoneme_id=phoneme_id)
    if note_id:
        p.note(note_id)
        return Target.note(note_id)
    if start_sec is None or end_sec is None:
        raise ProjectError("note_id / phoneme_id / (start_sec, end_sec) のどれかが要る")
    t0, t1 = _range(start_sec, end_sec)
    return Target.range(t0, t1)


def _tool(fn=None, *, lock=True):
    """例外を JSON に畳んで返す（MCP のエラーにすると原因が分かりにくいため）。

    `functools.wraps` を付けること。付けないと MCP 側がラッパの `(*a, **kw)` から
    スキーマを作ってしまい、全ツールが「a と kw が必須」になる。

    **`lock=False` は「走っているジョブを邪魔しない」ツール用**（`get_job` /
    `engine_info`）。ジョブは `_lock` を握ったまま走るので、`get_job` まで
    `_lock` を取ると**進捗を見に行った側がジョブの終わりまで止まる**。
    ジョブにした意味が無くなる（画面のステータスが動かず、MCP クライアントは
    タイムアウトする）。ここは辞書を読むだけなので GIL で足りる。
    """
    def deco(f):
        sig = inspect.signature(f)
        has_author = "author" in sig.parameters

        @functools.wraps(f)
        def wrapper(*a, **kw):
            t0 = time.perf_counter()
            if not bridge.is_app():
                # AI のプロセス（画面が起動したエンジンでない）: 画面の「AI に許可」に従う（bridge.py）。
                # 編集の author は必ず "ai"（画面の履歴で AI の操作に印を付ける。apply_plan の既定は human）
                try:
                    ba = sig.bind_partial(*a, **kw)
                except TypeError:
                    ba = None                   # 引数の誤りは下の呼び出しでいつもどおりのエラーにする
                if ba is not None:
                    no = bridge.denied(f.__name__, dict(ba.arguments))
                    if no is not None:
                        log.get("tool").info("%s denied (%s)", f.__name__, no["permission"])
                        return no
                    if has_author:
                        ba.arguments["author"] = "ai"
                        a, kw = ba.args, ba.kwargs
            try:
                if lock:
                    with _lock:
                        r = f(*a, **kw)
                        if (f.__name__ in HISTORY_TOOLS and isinstance(r, dict)
                                and r.get("ok") and "history" not in r):
                            r["history"] = _history_summary()
                        if (f.__name__ in HISTORY_TOOLS and isinstance(r, dict)
                                and r.get("ok") and "document" not in r):
                            r["document"] = _document_info()      # 未保存か（画面のタイトルの「*」。issue #33）
                else:
                    r = f(*a, **kw)
                log.get("tool").info("%s ok (%.2f s)", f.__name__,
                                     time.perf_counter() - t0)
                return r
            except Exception as e:      # noqa: BLE001
                log.get("tool").error("%s failed: %s\n%s", f.__name__, e,
                                      traceback.format_exc())
                from .prep import PreparationPending
                return {"ok": False, "error": str(e), "tool": f.__name__,
                        "conflict": isinstance(e, ProjectConflict),
                        "preparing": isinstance(e, PreparationPending), "log": log.current_log_file()}
        return wrapper
    return deco(fn) if fn is not None else deco


# 結果に曲の取り消しの履歴の要約（`history`: can_undo / can_redo / undo・redo のラベル）を付けるツール。
# 画面はこれで「元に戻す: ○○」を出す
HISTORY_TOOLS = {
    "set_lyrics", "import_lyrics", "shift_pitch", "set_pitch_curve", "set_transition", "split_note", "merge_notes",
    "move_note", "stretch", "move_boundary", "correct_to_guide", "apply_plan", "set_connection",
    "reset_to_original", "mute_notes", "set_fade", "set_tempo", "undo", "redo", "export_view_data", "list_changes", "list_tracks",
    "select_track", "add_track", "remove_track", "set_track", "set_guide_track", "open_project",
    "new_project", "load_project", "save_project",
}


def _history_summary():
    try:
        from . import mcp_tracks
        return mcp_tracks.history_summary()
    except Exception as e:                  # noqa: BLE001  要約が作れなくてもツールは返す
        log.get().warning("履歴の要約を作れない: %s", e)
        return None


def _document_info():
    try:
        from . import mcp_document
        return mcp_document.info()
    except Exception as e:                  # noqa: BLE001  状態が作れなくてもツールは返す
        log.get().warning("プロジェクトの状態を作れない: %s", e)
        return None


def _prep_yield():
    """表の重い処理（再生前の音作り・試聴・書き出し）の間、裏の準備（issue #63）は次の段へ進まない。"""
    from . import prep
    return prep.foreground()


def _rec(p, cs, label, group=None):
    """編集の changeset を曲の取り消しの履歴に入れる（`mcp_tracks.record_edit`）。label は画面に出す短い名前。"""
    if cs is None:
        return
    from . import mcp_tracks
    mcp_tracks.record_edit(p, cs, label, group=group)


class JobCancelled(Exception):
    """ジョブが安全な境界で中断を受け付けた。"""


def _submit_job(name, fn, cancellable=False, locked=True, info=None):
    """長い処理をジョブにする。locked=False はエンジンのロックを握らずに走る
    （音声認識・重みのダウンロードのように、プロジェクトに触れず長く掛かるもの。issue #54。
    裏の準備に合流した analyze_take。issue #63）。info: get_job に載せる固定の値。"""
    jid = "job-%s" % uuid.uuid4().hex[:8]
    cancel = threading.Event()
    with _jobs_lock:
        _jobs[jid] = {"id": jid, "name": name, "status": "running",
                      "started_at": time.time(), "result": None, "error": None,
                      "cancel": cancel, "cancellable": cancellable, "progress": None,
                      "stage": None, "info": dict(info or {})}

    def report(value, stage=None):
        """進み具合（0〜1）と、分かれば段の名前（`prep.STAGE_LABELS`）。"""
        with _jobs_lock:
            if cancel.is_set():
                raise JobCancelled()
            if value is not None:
                _jobs[jid]["progress"] = max(0.0, min(1.0, float(value)))
            if stage is not None:
                _jobs[jid]["stage"] = stage

    def commit(action):
        """Cancel and the first persistent commit share one decision point."""
        with _jobs_lock:
            if cancel.is_set():
                raise JobCancelled()
            _jobs[jid]["cancellable"] = False
            return action()

    def run():
        try:
            with (_lock if locked else contextlib.nullcontext()):
                if cancel.is_set():
                    raise JobCancelled()
                res = fn(cancel, report, commit) if cancellable else fn()
            with _jobs_lock:
                if cancel.is_set() and _jobs[jid]["cancellable"]:
                    raise JobCancelled()
                _jobs[jid].update(status="done", result=res, finished_at=time.time(), cancellable=False)
        except JobCancelled:
            with _jobs_lock:
                _jobs[jid].update(status="canceled", finished_at=time.time(), cancellable=False)
        except Exception as e:      # noqa: BLE001
            log.get("job").error("%s: %s\n%s", name, e, traceback.format_exc())
            with _jobs_lock:
                _jobs[jid].update(status="error", error=str(e), finished_at=time.time(), cancellable=False)

    threading.Thread(target=run, daemon=True, name=jid).start()
    with _jobs_lock:
        return {"ok": True, "status": "running", "job_id": jid, "tool": name,
                "cancellable": _jobs[jid]["cancellable"], "progress": _jobs[jid]["progress"],
                "hint": "get_job(job_id) で結果を取る。数秒おきに見ればよい。"}


# ---------------------------------------------------------------- 測る
@_tool
def open_project(take_path: str, guide_path: str = None, project_dir: str = None,
                 reuse: bool = True, lyrics: str = None,
                 guide_lyrics: str = None, offset_sec: float = None, length_sec: float = None,
                 source_id: str = None, guide_offset_sec: float = None,
                 guide_length_sec: float = None, author: str = "ai") -> dict:
    """テイク（とガイド）を開いてプロジェクトを作る／開き直す。

    take_path: 編集したい音声（WAV）の絶対パス（= ソース）
    offset_sec / length_sec: **ファイルの途中から切り出したクリップ**を編集するとき、ソース内の開始秒と長さ
        （省略時はファイル全体。段階 2 までと同じ）。プロジェクトの秒は**クリップの頭が 0**。
        返り値の take.offset_sec がソース上の開始位置
    source_id: ソースの ID（DAW のオーディオソースの ID など。省略時は中身の SHA-256 の頭）
    guide_offset_sec / guide_length_sec: ガイドもファイルの一部なら同じように
    guide_path: ガイドボーカル（任意）。あると list_deviations / correct_to_guide が使える
    project_dir: プロジェクトの置き場（省略時は <リポジトリ>/projects/<名前>-<hash>。配布版は %LOCALAPPDATA%\Gliss\projects）
    lyrics: テイクの歌詞（任意）。かな・カナ・漢字混じりのどれでもよい。
        例: "さくらさくら やよいのそらは"。与えると analyze_take で
        音素アラインメント（HubertFA）が走り、get_phonemes / move_boundary が使える
    guide_lyrics: ガイドの歌詞（任意）。**あるとタイミング補正の精度が上がる**
        （ガイドを直接アラインできるので、DTW で境界を写さずに済む）
    """
    from .media import Clip
    take = take_path
    if offset_sec is not None or length_sec is not None or source_id:
        take = Clip(take_path, offset_sec=offset_sec, length_sec=length_sec, source_id=source_id)
    guide = guide_path
    if guide_path and (guide_offset_sec is not None or guide_length_sec is not None):
        guide = Clip(guide_path, offset_sec=guide_offset_sec, length_sec=guide_length_sec)
    # テイクのプロジェクトのディレクトリにセッション（複数トラック。issue #7）を開き、テイクを編集対象にする。
    # 旧形式（issue #33 より前の projects/…）。そのディレクトリを名前を付けて保存済みなら、その .gliss を開く
    from . import mcp_document, mcp_tracks
    kind, r = mcp_document.open_legacy(take, guide, project_dir, reuse=reuse, lyrics=lyrics,
                                       guide_lyrics=guide_lyrics, author=author)
    if kind == "gliss":
        s, p, why, extra = r
        return mcp_document._result(s, p, why, opened="gliss", lyrics=(p.lyrics or None) if p else None,
                                    note="名前を付けて保存したプロジェクトを開いた", **extra)
    s, p, last = r
    mcp_tracks.adopt(s, p, s.current if s is not None else None)
    why = None
    if s is not None:
        _g, why = s.guide_clip_for(s.track(s.current))
    return _ok(project_dir=p.dir, log=p.log_path, take=p.take, guide=p.guide,
               edits=len(p.edits), changesets=len(p.changesets),
               analyzed=mcp_tracks.analyzed(s, p),
               lyrics=p.lyrics or None,
               lyrics_text={k: p.lyrics_text(k) for k in p.lyrics} or None,
               guide_note=why,
               session=mcp_tracks.summary(s, {"last_current": last}),
               document=mcp_document.info(), opened="legacy",
               next="analyze_take を呼ぶ（解析済みならそのまま list_notes / list_deviations）")


@_tool
def set_lyrics(text: str = None, source: str = "take", start_sec: float = None,
               end_sec: float = None, entries: list = None, from_text: str = None,
               mode: str = "replace", reanalyze: bool = True, author: str = "ai") -> dict:
    """歌詞を**区間ごと**に与える（かな・カナ・漢字混じり可）。音素アラインメントの入力。

    曲全体のテイク（例: 158 秒で歌うのはアウトロだけ）を想定しているので、
    歌詞は `{start_sec, end_sec, text}` の**配列**で持つ。アラインメントは
    **区間ごとに**掛かり、区間の外は音素を付けない（無音・息・歌詞の無い発声として扱う）。

    | 呼び方 | 動き |
    |---|---|
    | `set_lyrics(entries=[{start_sec, end_sec, text}, ...])` | **配列をまるごと置き換える**（推奨） |
    | `set_lyrics(text, start_sec, end_sec)` | その区間に 1 件入れる（重なる区間は消える） |
    | `set_lyrics("", start_sec, end_sec)` | その区間の歌詞を**消す** |
    | `set_lyrics(text)` | 範囲なし = 素材全体の歌詞 1 件（段階2 までと同じ） |
    | `set_lyrics("")` | 全部消す |
    | `set_lyrics(from_text=...)` | 歌詞テキストの中身をそのまま渡す（1 行 = `開始 終了 歌詞`） |

    引数:
      text: 1 区間ぶんの歌詞。例 "さくらさくら"。漢字は pyopenjtalk-plus の読みで開く
      entries: 区間の配列。`[{"start_sec": 149.19, "end_sec": 150.56, "text": "さくら…"}]`
      from_text: 歌詞ファイルの中身。1 行 1 区間で `144.30 148.77 やよいの…`（`2:24.30` も可）。
        時刻の無い行しか無ければ素材全体の歌詞 1 件になる。`#` の行と空行は飛ばす
      start_sec / end_sec: text を入れる区間（両方そろえる）
      source: "take"（既定）か "guide"。**ガイドにも入れると**タイミング補正が直接アラインになる
      mode: "replace"（既定。重なる区間を消してから入れる）か "add"（重なりを許さず足す）
      reanalyze: すぐアラインし直す（既定 True）

    長音「ー」は直前の母音に吸収してからアラインする（段階2 の実測で、
    `トマー` を `t o m a a` と書くと 2 つ目の母音が 8〜10 ms に潰れるため）。
    """
    from .phoneme import lyrics as LY
    p = _project()
    p.reload_if_changed()
    if from_text is not None:
        want = LY.normalize(LY.parse_text_file(from_text))
    elif entries is not None:
        want = LY.normalize(entries)
    else:
        want = LY.upsert(p.lyrics.get(source, []), text or "", start_sec=start_sec,
                         end_sec=end_sec, mode=mode)
    # 入力した区間だけ確定し、同じ配列に残った未修正の推定は維持する。
    if source == "take":
        old_entries = p.lyrics_entries("take")
        confirmed = []
        for e in want:
            old_same = next((o for o in old_entries if o["start_sec"] == e["start_sec"]
                             and o["end_sec"] == e["end_sec"]
                             and o["text"] == e["text"]), None)
            touched = (entries is not None or from_text is not None
                       or start_sec is None or e["start_sec"] == start_sec
                       and e["end_sec"] == end_sec)
            if old_same and not touched:
                confirmed.append(dict(old_same))
                continue
            out_e = dict(e, origin="confirmed")
            out_e.pop("confirmed_syllables", None)
            if not out_e.get("estimate") and e["start_sec"] is not None:
                old = next((o for o in old_entries if o.get("estimate")
                            and o["start_sec"] is not None
                            and o["start_sec"] < e["end_sec"]
                            and o["end_sec"] > e["start_sec"]), None)
                if old:
                    out_e["estimate"] = dict(old["estimate"])
            confirmed.append(out_e)
        want = LY.normalize(confirmed)
    # 取り消せる変更として入れる（changeset に前後の歌詞。undo で前の歌詞に戻る）
    cs, ent = p.change_lyrics(want, source=source, author=author)
    _rec(p, cs, "歌詞")
    out = {"source": source, "entries": ent, "n_entries": len(ent),
           "lyrics": p.lyrics_text(source) or None,
           "changeset": cs.id if cs else None}
    if ent and reanalyze:
        p.ensure_analyzed()
        res = p.analyze_phonemes(source, force=True)
        p._save_analysis()
        _invalidate_renderer()
        if res is not None:
            out.update(kana=res.kana,
                       phonemes=len([x for x in res.phonemes if x.label != "silence"]),
                       boundaries=len(res.boundaries),
                       confidence=round(res.confidence, 3),
                       warnings=[w["message"] for w in res.warnings[:5]],
                       aligner=res.aligner.get("name"), rtf=res.aligner.get("rtf"),
                       elapsed_sec=res.aligner.get("elapsed_sec"),
                       aligned_sec=res.aligner.get("aligned_sec"),
                       per_entry=[{"index": e["index"], "start_sec": e["start_sec"],
                                   "end_sec": e["end_sec"], "kana": e["kana"],
                                   "phonemes": e["n_phonemes"]} for e in res.entries])
    out["next"] = "get_phonemes で音素と境界を見る／move_boundary で境界を動かす"
    return _ok(**out)


@_tool
def inspect_lyrics_score(path: str) -> dict:
    """SVP / MIDI の歌詞付きトラック・音符数・テンポ・拍子を読む（プロジェクト不要）。"""
    from . import score_import

    score = score_import.read_score(path)
    return _ok(format=score["format"], tempo=score["tempo"], meter=score["meter"],
               tracks=[{"index": t["index"], "name": t["name"], "notes": len(t["notes"]),
                        "lyric_notes": sum(bool(n["lyric"]) and n["lyric"] not in ("-", "+", "br", "sil", "pau")
                                           for n in t["notes"])} for t in score["tracks"]])


@_tool
def import_lyrics(path: str, track: int | str = None, dry_run: bool = False,
                  song_start_sec: float = None, reanalyze: bool = True,
                  author: str = "ai") -> dict:
    """SVP / 歌詞付き MIDI を録音に対応付け、確定歌詞として読み込む。

    track: トラックの番号（0 始まり）または名前。歌詞のあるトラックが 1 つなら省略可。
    song_start_sec: 録音ファイルの頭の曲上の時刻。省略時は WAV の bext TimeReference。
      bext が無ければ 0 秒と仮定して警告する。クリップならソース上の開始位置も足す。
    dry_run: 対応の一覧だけ返し、歌詞は変更しない。
    reanalyze: 歌詞を登録した直後に音素をアラインする（既定 True）。

    声と発音の頭・音高の対応が足りない区間は登録せず unmatched に返す。
    既存の歌詞は set_lyrics(entries=…) と同様に全件置き換える（undo 可）。
    """
    from . import bwf, score_import

    p = _project()
    p.reload_if_changed()
    score = score_import.read_score(path)
    candidates = [t for t in score["tracks"] if score_import.lyric_text(t["notes"])]
    if track is None:
        if len(candidates) != 1:
            raise score_import.ScoreImportError(
                "歌詞のあるトラックを track で指定する: %s" %
                [(t["index"], t["name"]) for t in candidates])
        selected = candidates[0]
    elif isinstance(track, int):
        selected = next((t for t in score["tracks"] if t["index"] == track), None)
    else:
        selected = next((t for t in score["tracks"] if t["name"] == track), None)
    if selected is None or not score_import.lyric_text(selected["notes"]):
        raise score_import.ScoreImportError("指定したトラックに歌詞付き音符が無い")
    warning = []
    if song_start_sec is None:
        meta = bwf.read_meta(p.take["path"])
        if meta.time_reference is None:
            song_start = 0.0
            warning.append("録音 WAV に bext TimeReference が無い。曲の 0 秒からと仮定")
        else:
            song_start = meta.time_reference / float(p.take["sr"])
    else:
        song_start = float(song_start_sec)
    song_start += float(p.take.get("offset_sec") or 0)
    if not np.isfinite(song_start):
        raise score_import.ScoreImportError("song_start_sec は有限の秒で指定する")
    p.ensure_analyzed()
    bext_start = song_start
    if song_start_sec is None:
        song_start, origin_check = score_import.estimate_song_start(
            selected["notes"], p.take_notes, song_start)
        if origin_check["source"] == "score_pitch":
            warning.append("bext の位置と譜面・録音の音高列が一致しないため、曲の開始位置を %.4f 秒に補正" % song_start)
        elif origin_check.get("reason"):
            warning.append(origin_check["reason"])
    else:
        origin_check = {"source": "manual"}
    entries, matched, unmatched = score_import.match_to_take(
        selected["notes"], p.take_notes, p.onsets(), p.duration_sec, song_start)
    out = {"format": score["format"], "track": {"index": selected["index"],
           "name": selected["name"]}, "tracks": [{"index": t["index"], "name": t["name"],
           "notes": len(t["notes"])} for t in score["tracks"]],
           "tempo": score["tempo"], "meter": score["meter"],
           "song_start_sec": round(song_start, 6), "bext_start_sec": round(bext_start, 6),
           "origin_check": origin_check, "entries": entries,
           "matched": matched, "unmatched": unmatched,
           "warnings": warning, "dry_run": dry_run}
    if dry_run:
        return _ok(**out)
    if not entries:
        raise score_import.ScoreImportError("対応が確かな歌詞区間が無い。譜面のトラックと録音の位置を確認する")
    result = set_lyrics(entries=entries, source="take", reanalyze=reanalyze, author=author)
    if not result.get("ok"):
        return result
    return _ok(**out, changeset=result.get("changeset"),
               n_entries=result.get("n_entries"), phonemes=result.get("phonemes"),
               aligner=result.get("aligner"), document=_document_info())
@_tool
def get_lyrics(source: str = "take") -> dict:
    """歌詞を区間ごとに返す。未修正の推定は origin=estimated、部分修正は confirmed_syllables。"""
    p = _project()
    p.reload_if_changed()
    entries = [dict(e, origin=e.get("origin", "confirmed"))
               for e in p.lyrics_entries(source)]
    return _ok(source=source, entries=entries, text=p.lyrics_text(source))


@_tool
def list_utterances(start_sec: float = None, end_sec: float = None) -> dict:
    """発声区間の時刻・ノート・推定と現在の読み・確信度を返す。"""
    from .phoneme.auto_lyrics import utterance_ranges
    p = _project()
    p.reload_if_changed()
    p.ensure_analyzed()
    pres = p.phonemes("take") if p.has_lyrics("take") else None
    excluded = p.estimated_excluded_note_ids()
    out = []
    for a, b, ids in utterance_ranges(p.take_notes, p.duration_sec):
        if start_sec is not None and b <= start_sec:
            continue
        if end_sec is not None and a >= end_sec:
            continue
        ent = next((e for e in p.lyrics_entries("take")
                    if e["start_sec"] is None or
                    (e["start_sec"] < b and e["end_sec"] > a)), None)
        aligned = [] if pres is None or ent is None else [
            s for s in pres.syllables
            if ent["start_sec"] is None or
            (s["end_sec"] > ent["start_sec"] and s["start_sec"] < ent["end_sec"])]
        syll = [dict(s, confirmed=(ent.get("origin") != "estimated" or
                      i in ent.get("confirmed_syllables", [])))
                for i, s in enumerate(aligned) if s["end_sec"] > a and s["start_sec"] < b]
        out.append({"start_sec": a, "end_sec": b, "note_ids": ids,
                    "estimated_applied_note_ids": [i for i in ids if i not in excluded]
                    if ent and ent.get("origin") == "estimated" else [],
                    "current_lyrics": ent["text"] if ent else None,
                    "current_reading": (ent.get("reading") or ent["text"]) if ent else None,
                    "origin": ent.get("origin", "confirmed") if ent else None,
                    "estimated_reading": (ent.get("estimate") or {}).get("reading") if ent else None,
                    "confidence": (ent.get("estimate") or {}).get("confidence") if ent else None,
                    "syllables": syll})
    return _ok(utterances=out, total=len(out))


@_tool
def set_note_syllable(note_id: str, kana: str, syllable_index: int = None,
                      author: str = "ai") -> dict:
    """ノートに重なる音節 1 つを直す。複数あるノートは syllable_index を指定する。取り消せる。"""
    from .phoneme import g2p as G
    from .phoneme import lyrics as LY

    p = _project()
    p.reload_if_changed()
    p.ensure_analyzed()
    note = p.note(note_id)
    if not re.fullmatch(r"[ぁ-ゖァ-ヶ](?:[ゃゅょぁぃぅぇぉャュョァィゥェォー])?", kana):
        raise ProjectError("kana は 1 音節のかなにする（例: か / しゃ / ん）")
    reading = G.g2p(kana, use_pyopenjtalk=False)
    lex = [s for s in reading.syllables if s.romaji not in (G.SP, G.AP)]
    if len(lex) != 1 or not lex[0].kana:
        raise ProjectError("kana は 1 音節のかなにする（例: か / しゃ / ん）")
    res = p.phonemes("take")
    if res is None:
        raise ProjectError("音素が無い。先に analyze_take を呼ぶ")
    candidates = [s for s in res.syllables
                  if s["end_sec"] > note.start_sec and s["start_sec"] < note.end_sec]
    if syllable_index is None:
        if len(candidates) != 1:
            raise ProjectError("ノート %s に %d 音節が重なる。syllable_index を指定: %s"
                               % (note_id, len(candidates), [s["index"] for s in candidates]))
        target = candidates[0]
    else:
        target = next((s for s in candidates if s["index"] == syllable_index), None)
        if target is None:
            raise ProjectError("音節 %s はノート %s に重ならない" % (syllable_index, note_id))
    entries = p.lyrics_entries("take")
    ent_index = next((i for i, e in enumerate(entries)
                      if e["start_sec"] is None or
                      (e["start_sec"] < target["end_sec"] and
                       e["end_sec"] > target["start_sec"])), None)
    if ent_index is None:
        raise ProjectError("対象音節の歌詞区間が無い")
    entry = entries[ent_index]
    old = G.g2p(entry.get("reading") or entry["text"])
    syllables = [s for s in old.syllables if s.romaji not in (G.SP, G.AP)]
    aligned = [s for s in res.syllables
               if entry["start_sec"] is None or
               (entry["start_sec"] < s["end_sec"] and entry["end_sec"] > s["start_sec"])]
    local = next((i for i, s in enumerate(aligned) if s["index"] == target["index"]), None)
    if local is None or local >= len(syllables):
        raise ProjectError("歌詞の音節位置を特定できない。区間の歌詞を直す")
    syllables[local] = lex[0]
    new_reading = "".join(s.kana for s in syllables)
    updated = dict(entry)
    if entry.get("origin") != "estimated":
        updated["reading"] = new_reading
    else:
        updated["text"] = new_reading
    if entry.get("origin") == "estimated":
        marked = set(entry.get("confirmed_syllables", []))
        marked.add(local)
        if len(marked) >= len(syllables):
            updated["origin"] = "confirmed"
            updated.pop("confirmed_syllables", None)
        else:
            updated["confirmed_syllables"] = sorted(marked)
    else:
        updated["origin"] = "confirmed"
    entries[ent_index] = updated
    cs, normalized = p.change_lyrics(LY.normalize(entries), source="take", author=author,
                                     label="音節の修正")
    _rec(p, cs, "音節の修正")
    if cs:
        p.analyze_phonemes("take", force=True)
        p._save_analysis()
        _invalidate_renderer()
    return _ok(note_id=note_id, syllable_index=target["index"],
                before=entry.get("reading") or entry["text"],
                after=normalized[ent_index].get("reading") or normalized[ent_index]["text"],
                changeset=cs.id if cs else None)


@_tool
def analyze_take(force: bool = False, estimator: str = "rmvpe",
                 confidence_sweep: bool = False, background: bool = None) -> dict:
    """F0 → 音符のかたまり →（ガイドがあれば）DTW。結果はキャッシュする。

    estimator: "rmvpe"（正）/ "fcpe"（代替）/ "auto"
    confidence_sweep: 確信度を threshold 掃引で細かく出す（13 倍遅い）
    background: 省略時は長さから自動判断（20 秒を超えそうならジョブにする）。解析がキャッシュを読むだけで
    済むとき（準備済み・一度組んだガイド）は、True でもジョブにせずその場で結果を返す（issue #63）

    トラックの準備が裏で進んでいる・待っているとき（issue #63）は、その準備を最優先にして**合流**する
    （最初からやり直さない）。ジョブのときは待つ間エンジンのロックを握らないので、ほかのツールは動く。
    get_job の `joined` が True、`stage` / `stage_label` が今の段（テイクの音程・ガイドとの対応付け…）。
    合流したジョブを cancel_job しても、裏の準備そのものは続く。force・別の推定器・確信度の掃引のときは
    合流せず、表で計算する（裏の準備はそのトラックに触らない）。

    準備済みのキャッシュを読んで失敗した（JSON が壊れた・中身が足りない）ときは、そのファイルと準備済みの印を
    外して準備し直し、合流して待ってから開く（background のときはジョブ。エラーにはしない）。
    """
    from . import prep
    from . import mcp_tracks
    from .project.store import CacheBroken
    p = _project()
    p.reload_if_changed()
    est_sec = p.duration_sec * (0.45 * (13 if confidence_sweep else 1))
    if p.guide:
        est_sec += p.duration_sec * 1.2      # DTW の分
    asked = background
    if background is None:
        background = est_sec > JOB_THRESHOLD_SEC and not (
            os.path.exists(os.path.join(p.dir, "cache", "take-analysis.json")) and not force)
    default = not force and estimator == "rmvpe" and not confidence_sweep
    # キャッシュを読むだけで済むなら、background を頼まれてもジョブにせず、裏の準備にも合流せずにすぐ返す
    # （issue #63。画面は常に background で呼ぶので、準備済みのトラックでも 200 ms の確認を待っていた）
    cached = default and p.analysis_cached()
    tgt = mcp_tracks.prep_target(p)         # (セッション, トラック)。セッションのトラックでなければ None
    if cached and tgt is not None:
        sig = prep.track_sig(*tgt)
        pdir = tgt[0].project_dir_of(tgt[1])
        if prep.has_stamp(pdir, sig):
            cached = prep.is_ready(*tgt)
    if cached:
        background = False
    join = None
    if tgt is not None and default and not cached and prep.enabled() and not prep.is_ready(*tgt):
        join = (tgt[0].dir, tgt[1]["id"])

    def work(cancel=None, report=None, commit=None, retry=True, fresh=False):
        """retry: 壊れたキャッシュ（CacheBroken）に出会ったら、合流して作り直してから読み直す（同期の上限まで）。
        fresh: キャッシュを読むだけの近道を使わない（壊れたキャッシュを外した後）。"""
        q = p
        cur = _state["project"]
        if cur is not None and cur is not p and os.path.normcase(cur.dir) == os.path.normcase(p.dir):
            q = cur                          # 待っている間に同じトラックを開き直した（ガイドの指定など）
        q.reload_if_changed()                # 裏の準備が書いた歌詞の推定・解析を読む
        for attempt in range(4 if retry else 1):
            try:
                if default and cached and not fresh and attempt == 0:
                    q.analyze(cancel=cancel, progress=report, commit=commit)
                elif default:
                    # 表で計算する（準備に無い・失敗した）: 順番待ちの準備があれば合流して待ち、その後はトラックを
                    # 占有する（裏と表で同じ計算を 2 回しない。issue #63）
                    from .project import store
                    with store.front(q.dir):
                        q.analyze(cancel=cancel, progress=report, commit=commit)
                else:
                    ctx = prep.exclusive(q.dir) if tgt is not None else contextlib.nullcontext()
                    with ctx:
                        q.analyze(force=force, estimator=estimator, sweep=confidence_sweep,
                                  cancel=cancel, progress=report, commit=commit)
                break
            except CacheBroken:
                # 壊れたファイルは外し、印も取り消した（`Project._cache_broken`）。1 回ごとに 1 つ外れる
                if not retry or attempt == 3:
                    raise
        _invalidate_renderer()
        t2 = mcp_tracks.prep_target(q)
        if default and t2 is not None:
            prep.mark_ready(t2[0], t2[1], q)
        return _summary_of_analysis(q)

    def joined(cancel, report, commit):
        for attempt in range(4):
            prep.join(*join, report=lambda v, stage: report(0.95 * v, stage=stage), cancel=cancel)
            try:
                with _lock:
                    # 残りはキャッシュを読んで今の写しと project.json を書くだけ
                    return work(cancel=cancel, commit=commit, retry=False, fresh=attempt > 0,
                                report=lambda v: report(0.95 + 0.05 * v, stage="load"))
            except CacheBroken:
                # 準備済みのはずのキャッシュが壊れていた: 印を外して準備し直したので、もう一度合流して待つ
                # （エンジンのロックは握らずに待つ）
                if attempt == 3:
                    raise

    if join is not None and background:
        return _submit_job("analyze_take", joined, cancellable=True, locked=False,
                           info={"joined": True})
    if join is not None:
        prep.join(*join)                     # 同期: 準備の終わりを待つ（計算は 1 回）
    if background:
        return _submit_job("analyze_take", work, cancellable=True)
    try:
        return _ok(**work(retry=not (cached and asked)))
    except CacheBroken:
        if tgt is None or not prep.enabled():
            return _ok(**work(fresh=True))   # 準備に無い: 外したファイルを表で作り直す
        # 画面（background を頼んだ）: キャッシュを読むだけのはずが壊れていた。準備し直しに合流するジョブにする
        # （同期のまま待たない。ポップアップで進み具合を出す）
        join = (tgt[0].dir, tgt[1]["id"])
        return _submit_job("analyze_take", joined, cancellable=True, locked=False,
                           info={"joined": True})


def _summary_of_analysis(p):
    f0r = p.take_f0
    notes = p.take_notes
    pitched = [n for n in notes if n.kind == "note"]
    s = f0mod.summarize(f0r.f0, f0r.voiced, f0r.confidence)
    out = {
        "take": {"path": p.take["path"], "duration_sec": p.take["duration_sec"],
                 "sr": p.take["sr"], "channels": p.take["channels"]},
        "f0": dict(s, estimator=f0r.estimator, hop_ms=f0r.hop_s * 1000,
                   elapsed_sec=round(f0r.elapsed_sec, 2)),
        "notes": {"total": len(notes), "pitched": len(pitched),
                  "labels": _count([n.label for n in notes])},
        "phonemes": _phoneme_summary(p),
        "project_dir": p.dir,
    }
    if p.guide and p.alignment is not None:
        out["guide"] = {"path": p.guide["path"], "notes": len(p.guide_notes)}
        out["alignment"] = p.alignment.summary()
        info = getattr(p.alignment, "info", None) or {}
        if info.get("stage") == "fallback":
            out["guide_warning"] = "%s（%s）" % (info.get("note"), info.get("error"))
        out["next"] = "list_deviations でずれの一覧を見る"
    else:
        out["next"] = "list_notes / get_pitch / render_view"
    return out


def _count(xs):
    d = {}
    for x in xs:
        d[x] = d.get(x, 0) + 1
    return d


def _phoneme_summary(p):
    """analyze_take の返り値に入れる音素の要約。"""
    from .analysis.phonemes import backend_info
    if not p.has_lyrics("take"):
        return {"supported": True, "has_lyrics": False,
                "reason": "歌詞が無い。set_lyrics(text) を呼ぶと音素アラインメントが走る",
                "aligner": backend_info()["aligner"],
                "model_found": backend_info()["model_found"]}
    r = p.phonemes("take")
    if r is None:
        err = p.phoneme_error("take")
        if err:
            return {"supported": False, "has_lyrics": True, "error": err,
                    "reason": "音素アラインに失敗した（音素なしで続けている）: %s" % err}
        return {"supported": True, "has_lyrics": True, "reason": "未解析"}
    lab = _count([x.label for x in r.phonemes])
    out = {"supported": True, "has_lyrics": True, "kana": r.kana,
           "total": len(r.phonemes),
           "voiced": len([x for x in r.phonemes if x.label != "silence"]),
           "labels": lab, "boundaries": len(r.boundaries),
           "confidence": round(r.confidence, 3),
           "aligner": r.aligner.get("name"), "rtf": r.aligner.get("rtf"),
           "warnings": [w["message"] for w in r.warnings[:5]],
           "n_warnings": len(r.warnings),
           # 音素を切れなかった歌詞の区間（その区間は音素なしで続けている。issue #58）
           "failed_entries": list(r.aligner.get("failed_entries") or [])}
    g = p.phonemes("guide") if p.has_lyrics("guide") else None
    if g is not None:
        out["guide"] = {"voiced": len([x for x in g.phonemes if x.label != "silence"]),
                        "confidence": round(g.confidence, 3)}
    return out


@_tool
def get_pitch(start_sec: float = None, end_sec: float = None, source: str = "take",
              write_npy: bool = False) -> dict:
    """範囲の F0 の要約統計。**生の配列は返さない**（必要なら write_npy=True で NPY のパス）。"""
    p = _project()
    t0, t1 = _range(start_sec, end_sec)
    f0r = p.take_f0 if source == "take" else p.guide_f0
    if f0r is None:
        raise ProjectError("ガイドが無い")
    lo = int(round(t0 / f0r.hop_s))
    hi = int(round(t1 / f0r.hop_s))
    s = f0mod.summarize(f0r.f0, f0r.voiced, f0r.confidence, lo, hi)
    out = dict(s, ok=True, source=source, range_sec=[round(t0, 3), round(t1, 3)],
               hop_ms=f0r.hop_s * 1000, estimator=f0r.estimator)
    if write_npy:
        d = p.sub("cache")
        path = os.path.join(d, "f0-%s-%.3f-%.3f.npy" % (source, t0, t1))
        np.save(path, np.stack([f0r.f0[lo:hi], f0r.confidence[lo:hi],
                                f0r.voiced[lo:hi].astype("float64")]))
        out["npy_path"] = path
        out["npy_layout"] = "(3, n_frames) = [f0_hz, confidence, voiced]、10 ms ホップ"
    return out


@_tool
def list_notes(start_sec: float = None, end_sec: float = None, kind: str = "note",
               limit: int = 200) -> dict:
    """音符のかたまりを返す。kind: "note"（音程のあるもの）/ "all" / "unvoiced" など。"""
    p = _project()
    kinds = None if kind in (None, "all") else (kind,)
    ns = notes_in_range(p.take_notes, start_sec, end_sec, kinds)
    total = len(ns)
    ns = ns[:limit]
    pres = p.phonemes("take") if p.has_lyrics("take") else None
    hidden = set()
    if pres is not None:
        from .phoneme.lyrics import hidden_estimated_syllable_indices
        hidden = hidden_estimated_syllable_indices(
            p.lyrics_entries("take"), pres.syllables, p.take_notes,
            p.estimated_excluded_note_ids())
    from .project.fades import note_fades
    fades = note_fades(p)
    out = []
    for n in ns:
        d = {"id": n.id, "start_sec": n.start_sec, "end_sec": n.end_sec,
             "duration_sec": round(n.duration_sec, 3), "kind": n.kind,
             "label": n.label, "note": n.note_name, "pitch_hz": n.pitch_hz,
             "iqr_semitones": n.iqr_semitones, "rms_peak_db": n.rms_peak_db,
             "text": n.text, "confidence": n.confidence}
        if n.id in fades:                      # ノートのフェード（編集後の秒。set_fade）
            d["fade_in_sec"] = round(fades[n.id][0], 4)
            d["fade_out_sec"] = round(fades[n.id][1], 4)
        if pres is not None:
            ov = [x for x in pres.overlapping(n.start_sec, n.end_sec)
                  if x.syllable_index not in hidden]
            d["phonemes"] = [{"id": x.id, "text": x.text, "kana": x.kana,
                              "label": x.label,
                              "start_sec": x.start_sec, "end_sec": x.end_sec}
                             for x in ov]
            d["text"] = "".join(x.kana for x in ov if x.kana) or None
        out.append(d)
    return _ok(total=total, returned=len(out), truncated=total > len(out),
               phonemes_attached=pres is not None, notes=out)


@_tool
def list_deviations(threshold_cents: float = 50.0, threshold_ms: float = 30.0,
                    start_sec: float = None, end_sec: float = None,
                    limit: int = 100) -> dict:
    """ガイドとのずれ（セント / ms）の一覧。しきい値のどちらかを超えたノートだけ返す。

    歌詞があるときは **`timing`（音素境界ごとのタイミングのずれ）** も一緒に返す。
    `correct_to_guide` のタイミングが見ているのは発音の頭どうしの組
    （`plan_edit(op="guide")` の結果の `timing`）。ノートの timing_ms はノートの頭
    （音程の変わり目）と、ガイドのノートの頭を「ガイドの時刻 + 全体のずれ」に置いたものとの差。
    """
    p = _project()
    devs, meta = p.deviations(threshold_cents, threshold_ms)
    if start_sec is not None or end_sec is not None:
        t0, t1 = _range(start_sec, end_sec)
        devs = [d for d in devs if d.end_sec > t0 and d.start_sec < t1]
    total = len(devs)
    devs = sorted(devs, key=lambda d: -(abs(d.pitch_cents or 0) / max(threshold_cents, 1)
                                        + abs(d.timing_ms or 0) / max(threshold_ms, 1)))[:limit]
    out = {"total": total, "returned": len(devs),
           "thresholds": {"cents": threshold_cents, "ms": threshold_ms},
           "timing_detrend_ms": meta["timing_detrend_ms"],
           "note": "pitch_cents は + がテイクの方が高い。timing_ms は + がテイクの方が遅い"
                   "（曲全体のずれ = timing_detrend_ms は引いてある）",
           "deviations": [d.to_json() for d in devs]}
    if p.has_lyrics("take"):
        bd, bmeta = p.boundary_deviations()
        if start_sec is not None or end_sec is not None:
            t0, t1 = _range(start_sec, end_sec)
            bd = [d for d in bd if t0 <= d.take_sec <= t1]
        hit = [d for d in bd if d.timing_ms is not None and abs(d.timing_ms) >= threshold_ms]
        hit = sorted(hit, key=lambda d: -abs(d.timing_ms))[:limit]
        vals = [abs(d.timing_ms) for d in bd if d.timing_ms is not None]
        out["timing"] = {
            "unit": "音素境界", "source": bmeta["source"],
            "matched": bmeta["matched"], "total": len(bd),
            "timing_detrend_ms": bmeta["timing_detrend_ms"],
            "abs_median_ms": round(float(np.median(vals)), 1) if vals else None,
            "abs_max_ms": round(float(np.max(vals)), 1) if vals else None,
            "over_threshold": len([d for d in bd if d.timing_ms is not None
                                   and abs(d.timing_ms) >= threshold_ms]),
            "boundaries": [d.to_json() for d in hit],
            "note": ("ガイドにも歌詞があるので、ガイドを直接アラインした境界を使っている"
                     if bmeta["source"] == "guide_phonemes" else
                     "ガイドに歌詞が無いので、ガイドの音符の境目を DTW で対応づけている"
                     "（set_lyrics(source='guide') を入れると精度が上がる）"),
        }
    return _ok(**out)


@_tool
def get_phonemes(start_sec: float = None, end_sec: float = None,
                 source: str = "take", limit: int = 2000) -> dict:
    """音素と境界の一覧（時間範囲＋テキスト＋ラベル＋確信度）。

    歌詞が入っていないときは理由を返す（`set_lyrics` で与える）。
    返り値: phonemes（音素）/ syllables（かな 1 音節 = 1 ブロック）/
    boundaries（タイミング編集の制御点。`move_boundary` の引数はこの id）/ warnings。
    label は consonant / vowel / breath / silence の 4 値（detail に 8 値の内訳）。
    """
    return dict(_get_phonemes(_project(required=False), start_sec, end_sec,
                              source=source, limit=limit), ok=True)


# ---------------------------------------------------------------- 直す
def _add(kind, target, params, author, label, note=None, hist=None, group=None):
    p = _project()
    cs, edits = p.apply_edits([{"kind": kind, "target": target, "params": params, "note": note}],
                              author=author, label=label)
    _rec(p, cs, hist or label, group=group)
    return _ok(changeset=cs.id, edits=[e.to_json() for e in edits],
               total_edits=len(p.edits),
               next="render_preview / render_view / remeasure で確かめる。戻すなら undo")


@_tool
def shift_pitch(cents: float, note_id: str = None, start_sec: float = None,
                end_sec: float = None, author: str = "ai", note: str = None,
                group: str = None, label: str = None) -> dict:
    """音程を cents だけ動かす（+100 = 1 半音上）。対象は note_id か範囲。

    group: 同じ文字列を渡した続けての呼び出しは、取り消しの履歴で 1 回にまとめる
      （画面の複数ノートのピッチのドラッグ = Ctrl+Z 1 回で全部戻る）
    label: 取り消しの履歴に出す名前（既定「ピッチ」。画面の「半音に合わせる」など）
    """
    t = _resolve_target(note_id, start_sec, end_sec)
    return _add("pitch_shift", t, {"cents": float(cents)}, author,
                "%s を %+.0f セント" % (t.describe(), cents), note, hist=label or "ピッチ",
                group=group)


@_tool
def set_pitch_curve(points: list, note_id: str = None, start_sec: float = None,
                    end_sec: float = None, mode: str = "offset", ramp_ms: float = 40.0,
                    author: str = "ai", note: str = None) -> dict:
    """ピッチ曲線を与える。mode で意味が変わる:

    - "offset"（既定）: **ずらし量**。points は [[区間頭からの秒, セント], ...]（2 点以上）。
      対象は note_id か範囲。元の揺れに足される
    - "draw"（鉛筆）: **音程そのもの**。points は [[素材の秒（編集前）, MIDI ノート番号], ...]
      （例 [[149.30, 64.0], [149.45, 65.2]]）。その時間範囲のピッチ曲線を描いた線に**置き換え**、
      両端は ramp_ms（既定 40 ms）かけて元の曲線へなだらかにつなぐ。範囲は有声のフレームに
      切り詰める（無声は音程が無いので描いても効かない）。上から何度でも描き直せる
      （すっぽり覆われた前の線は外す）。戻すのは reset_to_original
    """
    if mode == "draw":
        from .project import pitch as PI
        p = _project()
        p.reload_if_changed()
        try:
            rm, specs, info = PI.draw_specs(p, points, ramp_sec=float(ramp_ms) / 1000.0)
        except PI.PitchError as e:
            raise ProjectError(str(e))
        cs = p.apply_changes(rm, specs, author=author,
                             label="%.3f–%.3f s にピッチを描いた" % (info["start_sec"],
                                                                 info["end_sec"]))
        _rec(p, cs, "鉛筆")
        return _ok(changeset=cs.id, total_edits=len(p.edits), **info,
                   next="render_preview / render_view で確かめる。戻すなら undo か reset_to_original")
    if mode != "offset":
        raise ProjectError("mode は offset か draw")
    t = _resolve_target(note_id, start_sec, end_sec)
    return _add("pitch_curve", t, {"points": points}, author,
                "%s にピッチ曲線 %d 点" % (t.describe(), len(points)), note, hist="ピッチ")


def _replace_changeset(p, replaces):
    """ポップアップ内の当て直し: 前回の changeset を取り消して捨てる（redo で戻さない）。"""
    if not replaces:
        return
    c = next((c for c in p.changesets if c.id == replaces), None)
    if c is not None:
        if not c.undone:
            p.undo(replaces)
        p.discard(replaces)
        from . import mcp_tracks
        mcp_tracks.forget_changeset(p, replaces)      # 履歴の項目も入れ替える（当て直し 1 回 = 取り消し 1 回）


@_tool
def set_transition(value: float = None, note_ids: list = None, note_a: str = None,
                   note_b: str = None, start_sec: float = None, end_sec: float = None,
                   replaces: str = None, author: str = "ai") -> dict:
    """**ノートの変わり目のなだらかさ**（Melodyne のピッチトランジション）。

    ピッチを動かしたノートと隣のノートの境目で、移動量の差を段差ではなく
    raised-cosine の曲線で渡す（原音の移り変わりの形は保ち、移動量の差だけを補間する）。
    **接続された境目だけ**に効く（切り離しの境目は段差のまま）。何もしなくても自動で効いている。

    value: 0 = 段差（補間しない）、0.5 = **自動**（既定。原音の境目で実際に音程が移り変わって
        いる長さを F0 から測って幅にする）、1 = かなりゆっくり（400 ms 以上）。
        省略（null）でも自動に戻す
    対象: note_a + note_b（その境目 1 つ）/ note_ids（そのノートの両側の境目）/
        範囲（境目の時刻が入るもの）/ 省略で全体
    replaces: 先に取り消す changeset（画面のスライダーの当て直し。ポップアップ 1 回 = 取り消し 1 回）
    境目ごとの値は project.json（編集リストの `transition`）に入る。一覧は list_connections。
    """
    from .project import pitch as PI
    p = _project()
    p.reload_if_changed()
    _replace_changeset(p, replaces)
    if value is not None and not (0.0 <= float(value) <= 1.0):
        raise ProjectError("value は 0〜1（0 = 段差、0.5 = 自動、1 = ゆっくり）")
    try:
        pairs = PI.transition_pairs(p, note_a, note_b, note_ids, start_sec, end_sec)
    except PI.PitchError as e:
        raise ProjectError(str(e))
    if not pairs:
        return _ok(changeset=None, pairs=0, message="接続された境目が無い")
    rm, add = PI.transition_specs(p, pairs, value)
    if not rm and not add:
        return _ok(changeset=None, pairs=len(pairs), value=value, message="変わらない（自動のまま）")
    v = PI.DEFAULT_VALUE if value is None else float(value)
    cs = p.apply_changes(rm, add, author=author,
                         label="つなぎのなだらかさ %.2f（%d 境目）" % (v, len(pairs)))
    _rec(p, cs, "なだらかさ")
    return _ok(changeset=cs.id, pairs=len(pairs), value=v, total_edits=len(p.edits),
               next="render_preview で聴く／render_view で見る。戻すなら undo")


@_tool
def split_note(sec: float, note_id: str = None, snap_ms: float = 0.0,
               author: str = "ai") -> dict:
    """ノートを sec（**編集前の秒**）で 2 つに分ける（Melodyne の分割ツール）。

    新しい境目は**接続**（隙間なし）。左は元の id、右は `<元の id>@<ミリ秒>`。
    分割したノートは、ピッチ・タイミング・なだらかさ・ガイドに合わせる、すべての編集で
    ふつうのノートとして扱える。**時刻で記録するので、解析し直しても分割は残る**。
    そのノートに掛かっていた編集は同じ区間に掛かったまま（音は変わらない）。
    snap_ms: 音素境界がこれ以内にあればそこで切る（既定 0 = 寄せない）
    両端から 20 ms 以上内側で切ること。戻すのは merge_notes か undo。
    """
    from .project import notes_edit as NE
    p = _project()
    p.reload_if_changed()
    try:
        rm, add, info = NE.split_specs(p, float(sec), note_id, float(snap_ms) / 1000.0)
    except NE.NoteEditError as e:
        raise ProjectError(str(e))
    cs = p.apply_changes(rm, add, author=author,
                         label="%s を %.3f s で分割" % (info["left"], info["sec"]))
    _rec(p, cs, "分割")
    return _ok(changeset=cs.id, **info, total_edits=len(p.edits))


@_tool
def merge_notes(note_a: str, note_b: str, author: str = "ai", group: str = None) -> dict:
    """接して並ぶ 2 つのノート（a の次が b、隙間なし）を 1 つにする（分割線のダブルクリック・Ctrl+J）。

    結合したノートは a の id。分割でできた境目なら、その分割を外すだけ。
    解析でできた境目でも結合できる（`merge` として記録。解析し直しても残る）。
    group: 同じ文字列を渡した続けての呼び出しは、取り消しの履歴で 1 回にまとめる
      （画面で 3 つ以上のノートを選んで結合 = Ctrl+Z 1 回で全部戻る）
    """
    from .project import notes_edit as NE
    p = _project()
    p.reload_if_changed()
    try:
        rm, add, info = NE.merge_specs(p, note_a, note_b)
    except NE.NoteEditError as e:
        raise ProjectError(str(e))
    cs = p.apply_changes(rm, add, author=author, label="%s と %s を結合" % (note_a, note_b))
    _rec(p, cs, "結合", group=group)
    return _ok(changeset=cs.id, **info, total_edits=len(p.edits))


@_tool
def move_note(ms: float, note_id: str = None, note_ids: list = None, start_sec: float = None,
              end_sec: float = None, author: str = "ai", note: str = None) -> dict:
    """ノートを ms だけ前後に動かす（+ が遅く、− が早く）。**後ろはずらさない**。

    - 接続された側の隣は伸び縮みして吸収する（境目を共有しているので）
    - 切り離された側は隙間が吸収する（広がったぶんは無音、狭まったぶんは隙間の音を切り取る）
    - 隣を追い越す手前・ノートが 20 ms を切る手前で止まる（`clamped` が true）
    対象は note_id / note_ids（複数を一緒に動かす）/ 範囲（範囲に頭が入るノート）。
    子音・息・無音の区間（list_notes(kind="all")）も id で渡せば動かせる。
    """
    from .project import timing as TM
    p = _project()
    p.reload_if_changed()
    ids = _note_ids(note_id, note_ids, start_sec, end_sec)
    plan = TM.plan_move(p, ids)
    return _apply_now(plan, float(ms) / 1000.0, author,
                      "%s を %+.0f ms" % (",".join(ids), ms), note=note, hist="ノートの移動")


@_tool
def stretch(ratio: float, note_id: str = None, start_sec: float = None, end_sec: float = None,
            phoneme_id: str = None, author: str = "ai", note: str = None) -> dict:
    """長さを ratio 倍にする（1.2 = 2 割長く）。0.25〜4.0。**後ろはずらさない**。

    - note_id: ノートの**尻**を動かす（画面で右端をドラッグするのと同じ）。
      尻が接続されていれば次のノートが縮み／伸び、切り離されていれば隙間が増減する
    - phoneme_id: その音素の後ろの境界を動かす（`move_boundary` と同じ。次の音素が吸収）
    - 範囲（1 つのノートの中）: 範囲を伸縮し、同じノートの残りが吸収する
    歌詞があるときは母音（と息・無音）だけが伸び縮みし、子音の長さは保つ。
    """
    from .project import timing as TM
    if not (0.25 <= float(ratio) <= 4.0):
        raise ProjectError("ratio は 0.25〜4.0")
    p = _project()
    p.reload_if_changed()
    if phoneme_id:
        res = p.phonemes("take")
        ph = p.phoneme(phoneme_id)
        b = next((x for x in res.boundaries if x.before_index == ph.index), None)
        if b is None:
            raise ProjectError("%s の後ろに境界が無い（素材の端）" % phoneme_id)
        cur = p.edited_length(ph.start_sec, ph.end_sec)
        return move_boundary.__wrapped__(b.id, (float(ratio) - 1.0) * cur * 1000.0,
                                         author=author, note=note)
    if note_id:
        plan = TM.plan_edge(p, note_id, "end")
        tm = TM.current_map(p)
        n = p.note(note_id)
        cur = tm.at(n.end_sec, "left") - tm.at(n.start_sec, "right")
        return _apply_now(plan, (float(ratio) - 1.0) * cur, author,
                          "%s を %.2f 倍" % (note_id, ratio), note=note, hist="ノートの長さ")
    if start_sec is None or end_sec is None:
        raise ProjectError("note_id / phoneme_id / (start_sec, end_sec) のどれかが要る")
    plan = TM.plan_range_stretch(p, float(start_sec), float(end_sec), float(ratio))
    return _apply_now(plan, plan.info["want_x"], author,
                      "%.3f–%.3f s を %.2f 倍" % (start_sec, end_sec, ratio), note=note,
                      hist="ノートの長さ")


def _note_ids(note_id=None, note_ids=None, start_sec=None, end_sec=None):
    from .project import timing as TM
    p = _project()
    if note_ids:
        return [str(i) for i in note_ids]
    if note_id:
        return [note_id]
    if start_sec is None or end_sec is None:
        raise ProjectError("note_id / note_ids / (start_sec, end_sec) のどれかが要る")
    ids = [n.id for n in TM.pitched_notes(p)
           if float(start_sec) <= n.start_sec < float(end_sec)]
    if not ids:
        raise ProjectError("範囲に音程のあるノートが無い")
    return ids


def _remember(plan):
    _plans[plan.id] = plan
    while len(_plans) > MAX_PLANS:
        _plans.pop(next(iter(_plans)))
    return plan


def _apply_now(plan, x, author, label, note=None, pitch=None, hist=None):
    """計画を作ってすぐ確定する（MCP の直接の編集ツール用）。hist: 取り消しの履歴に出す短い名前。"""
    from .project import timing as TM
    p = _project()
    want = x
    x = float(np.clip(x, plan.x_lo, plan.x_hi)) if x is not None else None
    cs, info = TM.apply_plan(p, plan, x, pitch=pitch, author=author, label=label)
    if hist == "ノートの長さ" and (info.get("snapped") or (plan.set_connections and x)):
        hist = "ノートの長さ（接続の変更）"
    _rec(p, cs, hist or label)
    out = {"changeset": cs.id if cs else None, "total_edits": len(p.edits),
           "x": None if x is None else round(x, 6),
           "clamped": x is not None and want is not None and abs(x - want) > 1e-9,
           "range": [round(plan.x_lo, 6), round(plan.x_hi, 6)],
           "snapped": bool(info.get("snapped")),
           "window_sec": info.get("window_sec"),
           "next": "render_preview / render_view / remeasure で確かめる。戻すなら undo"}
    if note and cs:
        out["note"] = note
    return _ok(**out)


@_tool
def move_boundary(boundary_id: str, ms: float, author: str = "ai",
                  note: str = None) -> dict:
    """**音素の境目**を ms だけ横に動かす（+ が遅く、− が早く）。

    タイミング編集の単位はこれ。1 本動かすと**隣り合う 2 つの音素の
    長さが同時に変わる**（全体の長さは変わらない）。どちらの音素も **20 ms** は下回らない
    （下回る指定は自動で切り詰め、結果の `clamped` が true になる）。

    boundary_id は `get_phonemes` の `boundaries[].id`（"b012" など）。
    ms は**編集後の時間軸**での移動量（画面でつまんだ距離そのもの）。
    """
    from .phoneme.edit import move_boundary_spec
    p = _project()
    p.reload_if_changed()
    spec, info = move_boundary_spec(p, boundary_id, ms)
    spec["target"] = Target.boundary(boundary_id, spec["params"]["left_sec"],
                                     spec["params"]["right_sec"])
    spec["note"] = note
    cs, edits = p.apply_edits([spec], author=author,
                              label="境界 %s を %+.0f ms" % (boundary_id, info["ms"]))
    _rec(p, cs, "音素の境界")
    return _ok(changeset=cs.id, edits=[e.to_json() for e in edits],
               total_edits=len(p.edits), moved=info,
               next="render_preview で聴く。戻すなら undo('%s')" % cs.id)


@_tool
def correct_to_guide(start_sec: float = None, end_sec: float = None,
                     pitch_strength: float = 0.7, timing_strength: float = 0.0,
                     threshold_cents: float = 0.0, threshold_ms: float = 0.0,
                     note_ids: list = None, author: str = "ai",
                     match_pitch_shape: bool = True) -> dict:
    """ガイドへ寄せる。強度 0〜1（1 = ガイドどおり）。1 つの changeset にまとめる。

    「ガイドどおり＝正解」ではないので、既定はピッチ 0.7・タイミング 0。
    画面の「ガイドに合わせる」と**同じ計画**（`plan_edit(op="guide")`）を使う:

    - ピッチ 100%: match_pitch_shape=True（既定）は発音の頭どうしと対応ノート内の比例時間で
      ガイド F0 を写し、線をフレームごとに近づける。無声・未対応は動かさない。
      False は従来どおりノートの平均音程を一定量ずらす
    - タイミング 100%: テイクの**発音の頭**（音の立ち上がり。歌詞がテイクとガイドの両方に
      あれば音節の頭）を、1 対 1 に対応する**タイムライン上のガイドの発音の頭**（画面に描くガイドの
      位置。トラックの位置ずらし `offset_sec` も含む。全体のずれが 150 ms を超える置き場所の違う素材だけ
      「ガイドの時刻 + 全体のずれ」。issue #61）へ。DTW で写した位置ではない（テイク自身のリズムに沿うので、
      合わせてもリズムが直らない。issue #12）。対応が決まらない頭（写像が揺れている・候補が
      2 つ・1 対多・ずれが大きすぎる）は動かさない。**接続の規則で動かすので後ろはずれない**。
      歌詞があれば母音（と息・無音）だけが伸び縮みし、子音の長さは保つ
    - **基準点と補間**（issue #53）: 確かな頭の組（と、テイク・ガイドとも確定の歌詞がある所の、休みの
      手前の音節の終わり）を基準点にし、その間のノート（発音の頭が無い音程の変わり目など）は前後の
      基準点に合わせて比例で動かす（同じフレーズの中、基準点の間が 1 秒以内）。基準点の無い所は動かさない
    - しきい値（既定 0 = 全部）は計画を作るときに掛ける
    対象: note_ids、または範囲（範囲に頭が入るノート）、省略で全体。

    結果の `correspondence` はノートごとの対応: guide（音程の対応。1 対多は同じ組の範囲）、
    confirmed（確かな頭の組で裏付けられた対応）、pitch（音程が寄る）、timing（anchor = 頭が基準点 /
    interp = 比例 / null = 動かさない）、reached（100% で目標に届く）、reason（対応やタイミングが無い理由）。
    `timing_possible` が false のガイド（別の演奏など）は `timing_message` にその旨が入る。
    """
    from .project import timing as TM
    p = _project()
    p.reload_if_changed()
    ids = None
    if note_ids or start_sec is not None or end_sec is not None:
        ids = _note_ids(None, note_ids,
                        0.0 if start_sec is None else start_sec,
                        p.duration_sec + 1 if end_sec is None else end_sec)
    plan = TM.plan_guide(p, ids, threshold_cents=float(threshold_cents),
                         threshold_ms=float(threshold_ms),
                         match_pitch_shape=bool(match_pitch_shape))
    r = _apply_now(plan, float(timing_strength) or None, author,
                   "ガイドへ寄せる（ピッチ %.0f%% / タイミング %.0f%%）"
                   % (pitch_strength * 100, timing_strength * 100),
                   pitch=float(pitch_strength) or None, hist="ガイドに合わせる")
    r.update(pairs=len(plan.pairs), matched_notes=plan.info.get("matched_notes"),
             unmatched_notes=plan.info.get("unmatched_notes"),
             confirmed_notes=plan.info.get("confirmed_notes"),
             timing_notes=plan.info.get("timing_notes"),
             timing_anchor_notes=plan.info.get("timing_anchor_notes"),
             timing_interp_notes=plan.info.get("timing_interp_notes"),
             timing_reached_notes=plan.info.get("timing_reached_notes"),
             timing_possible=plan.info.get("timing_possible"),
             correspondence=TM.correspondence_summary(plan),
             pitch_notes=len(plan.pitch), repaired=plan.info.get("repaired"),
             reach=plan.info.get("reach"),
             next="remeasure で残ったずれを見る。戻すなら undo('%s')" % r.get("changeset"))
    if plan.info.get("timing_message"):
        r["timing_message"] = plan.info["timing_message"]
    return r


# ---------------------------------------------------------------- 計画（画面と共有）
@_tool
def plan_edit(op: str, note_id: str = None, note_ids: list = None, side: str = None,
              detach: bool = False, start_sec: float = None, end_sec: float = None,
              threshold_cents: float = 0.0, threshold_ms: float = 0.0,
              match_pitch_shape: bool = True) -> dict:
    """**計画**を作る（まだ編集しない）。画面がドラッグの開始・「ガイドに合わせる」を開いた時点で呼ぶ。

    計画 = 「節（ノートの頭・尻・音素境界）の編集後の秒 = cur + d × x」。
    画面は x を動かしながら同じ式で描き、離したら `apply_plan(plan_id, x)` で確定する。
    **プレビューと確定が同じ計画を使う**ので、ドラッグ中の見た目と結果が一致する。

    op:
      "edge"  : ノートの端（side = "start" / "end"）。x = 秒。detach=True は Alt（接続を切って自分だけ）
      "move"  : ノート（note_id / note_ids）を横に動かす。x = 秒
      （edge / move は子音・息・無音の区間も可）
      "guide" : ガイドに合わせる（note_ids か範囲、省略で全体）。x = タイミングの強度 0〜1、
                ピッチは apply_plan の pitch に強度を渡す
    返り値: plan_id、x の範囲（隣を追い越す／短すぎる手前）、`snap_x`（切り離された端が隣に
    ぶつかる位置。ここで離すと接続になる）、中身の JSON のパス（画面が読む）。
    guide では `correspondence`（ノートごとの対応と理由。`correct_to_guide` と同じ）も返す。
    """
    from .project import timing as TM
    p = _project()
    p.reload_if_changed()
    if op == "edge":
        if not note_id or side not in ("start", "end"):
            raise ProjectError("edge には note_id と side（start / end）が要る")
        plan = TM.plan_edge(p, note_id, side, detach=bool(detach))
    elif op == "move":
        plan = TM.plan_move(p, _note_ids(note_id, note_ids, start_sec, end_sec))
    elif op == "guide":
        ids = None
        if note_ids or note_id or start_sec is not None or end_sec is not None:
            ids = _note_ids(note_id, note_ids,
                            0.0 if start_sec is None else start_sec,
                            p.duration_sec + 1 if end_sec is None else end_sec)
        plan = TM.plan_guide(p, ids, threshold_cents=float(threshold_cents),
                             threshold_ms=float(threshold_ms),
                             match_pitch_shape=bool(match_pitch_shape))
    else:
        raise ProjectError("op は edge / move / guide")
    _remember(plan)
    path = TM.write_plan(p, plan)
    extra = {}
    if plan.kind == "guide":
        extra["correspondence"] = TM.correspondence_summary(plan)
    return _ok(plan_id=plan.id, kind=plan.kind, path=path,
               x_range=[round(plan.x_lo, 6), round(plan.x_hi, 6)],
               snap_x=None if plan.snap_x is None else round(plan.snap_x, 6),
               moving_knots=int(np.sum(np.abs(plan.d) > 1e-12)),
               pitch_notes=len(plan.pitch), pairs=len(plan.pairs), info=plan.info, **extra,
               hint="画面向けの中身は path の JSON。確定は apply_plan(plan_id, x)")


@_tool
def apply_plan(plan_id: str, x: float = None, pitch: float = None, replaces: str = None,
               author: str = "human", label: str = None) -> dict:
    """`plan_edit` で作った計画を x（と pitch）で確定する。1 つの changeset。

    x: edge / move は秒、guide はタイミングの強度（0〜1）。範囲の外は端に丸める
    pitch: guide のピッチの強度（0〜1）
    replaces: 先に取り消す changeset（「ガイドに合わせる」のポップアップ内の当て直し。
        取り消した後の状態 = 計画を作ったときの状態なので、同じ計画がそのまま当たる）
    label: 取り消しの履歴に出す名前（既定は「ノートの長さ」など。画面の右クリック「つなぐ」が使う）
    """
    from .project import timing as TM
    p = _project()
    p.reload_if_changed()
    plan = _plans.get(plan_id)
    if plan is None:
        raise ProjectError("計画が無い: %s（plan_edit で作り直す）" % plan_id)
    _replace_changeset(p, replaces)      # 当て直しで捨てた changeset は redo で戻さない
    replanned = False
    if plan.sig != TM.state_sig(p):
        plan = _remember(_replan(p, plan))
        replanned = True
    t = None if x is None or float(x) == 0.0 else float(x)
    pv = None if pitch is None or float(pitch) == 0.0 else float(pitch)
    desc = {"edge": "端を %+.0f ms", "move": "移動 %+.0f ms"}.get(plan.kind)
    desc = (desc % ((t or 0.0) * 1000)) if desc else (
        "ガイドへ寄せる（ピッチ %.0f%% / タイミング %.0f%%）" % ((pv or 0) * 100, (t or 0) * 100))
    hist = label or {"edge": "ノートの長さ", "move": "ノートの移動"}.get(plan.kind, "ガイドに合わせる")
    r = _apply_now(plan, t, author, desc, pitch=pv, hist=hist)
    r["replanned"] = replanned
    r["plan_id"] = plan.id
    return r


def _replan(p, plan):
    """状態が変わっていたら（外から編集された）同じ引数で計画を作り直す。"""
    from .project import timing as TM
    k, a = plan.kind, plan.params
    if k == "edge":
        return TM.plan_edge(p, a["note_id"], a["side"], detach=a.get("detach", False))
    if k == "move":
        return TM.plan_move(p, a["note_ids"])
    if k == "guide":
        return TM.plan_guide(p, a.get("note_ids"), a.get("threshold_cents", 0.0),
                             a.get("threshold_ms", 0.0), a.get("match_pitch_shape", True))
    if k == "range":
        return TM.plan_range_stretch(p, a["start_sec"], a["end_sec"], a["ratio"])
    if k == "reset":
        return TM.plan_reset_timing(p, a["note_ids"])
    raise ProjectError("作り直せない計画: %s" % k)


@_tool
def list_connections(start_sec: float = None, end_sec: float = None) -> dict:
    """隣り合う音程ノートの**接続 / 切り離し**と、**つなぎのなだらかさ**の一覧。

    接続 = 境目を共有（片方を縮めると隣が伸びる）。切り離し = 隙間が増減する。
    既定: 間に何も無い／無声（子音）だけで 0.30 秒未満なら接続、息・無音を挟めば切り離し。
    `default` と違うものは `set_connection` か画面（Alt+ドラッグ・吸着）で変えたもの。
    """
    from .project import pitch as PI
    from .project import timing as TM
    p = _project()
    p.reload_if_changed()
    all_trs = PI.transitions(p)
    trs = {(t.a, t.b): t for t in all_trs if t.kind == "boundary"}
    rows = []
    for a, b, c, dflt in TM.connections(p):
        if start_sec is not None and b.start_sec < float(start_sec):
            continue
        if end_sec is not None and a.end_sec > float(end_sec):
            continue
        row = {"a": a.id, "b": b.id, "connected": c, "default": dflt,
               "gap_ms": round((b.start_sec - a.end_sec) * 1000, 1),
               "at_sec": round(a.end_sec, 4)}
        t = trs.get((a.id, b.id))
        if t is not None:
            row["transition"] = _tr_row(t)
        rows.append(row)
    steps = []
    for t in all_trs:
        if t.kind != "step":
            continue
        if start_sec is not None and t.ta < float(start_sec):
            continue
        if end_sec is not None and t.ta > float(end_sec):
            continue
        steps.append(dict(note=t.a, at_sec=round(t.ta, 4), transition=_tr_row(t)))
    return _ok(connections=rows, total=len(rows), steps=steps,
               note="transition はつなぎのなだらかさ（set_transition）。step_cents = 移動量の段差"
                    "（0 なら補間するものが無い）。切り離しの境目には無い。steps はノートの中の段差"
                    "（結合した元の境目など）に掛かっているなだらかさ（set_transition の note_ids で変えられる）")


def _tr_row(t):
    from .project import pitch as PI
    return {"value": round(t.value, 3), "auto": not t.set_by_user,
            "auto_ms": round(t.auto_sec * 1000, 1),
            "width_ms": round((t.hl + t.hr) * 1000, 1),
            "step_cents": round(t.delta, 1),
            "smoothing": bool(abs(t.delta) > PI.EPS_CENTS and t.hl + t.hr > 0)}


@_tool
def set_connection(note_a: str, note_b: str, connected: bool, author: str = "ai") -> dict:
    """隣り合う 2 つのノート（a の次が b）の接続を変える。**音は変わらない**（次の編集の動き方が変わる）。"""
    from .project import timing as TM
    p = _project()
    p.reload_if_changed()
    ns = TM.pitched_notes(p)
    ix = {n.id: i for i, n in enumerate(ns)}
    if note_a not in ix or note_b not in ix or ix[note_b] != ix[note_a] + 1:
        raise ProjectError("%s の次の音程ノートが %s ではない（list_connections で確認）"
                           % (note_a, note_b))
    rm, add = TM.connection_specs(p, [(note_a, note_b, bool(connected))])
    cs = p.apply_changes(rm, add, author=author, label="%s｜%s を%s" % (
        note_a, note_b, "接続" if connected else "切り離し"))
    _rec(p, cs, "つなぐ" if connected else "切り離し")
    return _ok(changeset=cs.id, a=note_a, b=note_b, connected=bool(connected))


def _mute_spans(p):
    return [(float(e.target.start_sec), float(e.target.end_sec), e.id)
            for e in p.edits if e.kind == "mute"]


def _trim_mutes(p, spans):
    """無音（mute）を spans の分だけ外す。(外す id, 入れる spec)。範囲の外の部分は残す。"""
    from .project.pitch import _outside
    rm, add = [], []
    for a, b, eid in _mute_spans(p):
        if not any(t > a + 1e-9 and s < b - 1e-9 for s, t in spans):
            continue
        rm.append(eid)
        for x, y in _outside(a, b, spans):
            add.append({"kind": "mute", "target": Target.range(x, y), "params": {}})
    return rm, add


@_tool
def mute_notes(note_ids: list = None, start_sec: float = None, end_sec: float = None,
               author: str = "ai") -> dict:
    """ノートを**無音にする**（画面の右クリック「無音にする」・Del）。1 つの changeset。

    長さと位置は変えない（後ろはずらさない）。その区間の音だけが消え、隣とは 20 ms で無音へ
    フェードする。ピッチなどの編集は残る（オリジナルに戻す・undo で音が戻る）。
    対象: note_ids（音程ノートの id）か範囲（重なるノート）。もう無音のノートは飛ばす。
    `export_view_data` の `notes[].muted` が true になる。
    """
    p = _project()
    p.reload_if_changed()
    ids = list(note_ids or [])
    if start_sec is not None or end_sec is not None:
        t0, t1 = _range(start_sec, end_sec)
        ids += [n.id for n in p.take_notes if n.kind == "note" and n.end_sec > t0
                and n.start_sec < t1 and n.id not in ids]
    by = {n.id: n for n in p.take_notes}
    missing = [i for i in ids if i not in by]
    if missing:
        raise ProjectError("ノートが無い: %s（list_notes で確認）" % ", ".join(missing))
    spans = _mute_spans(p)
    specs, done = [], []
    for i in ids:
        n = by[i]
        if n.end_sec - n.start_sec <= 1e-6:
            continue
        cov = sum(max(0.0, min(b, n.end_sec) - max(a, n.start_sec)) for a, b, _ in spans)
        if cov >= (n.end_sec - n.start_sec) - 1e-6:
            continue                                  # もう無音
        specs.append({"kind": "mute", "target": Target.range(n.start_sec, n.end_sec),
                      "params": {}})
        done.append(i)
    if not specs:
        return _ok(changeset=None, note_ids=[], message="無音にするノートが無かった（もう無音）")
    cs, edits = p.apply_edits(specs, author=author, label="無音にする（%d ノート）" % len(done))
    _rec(p, cs, "無音にする")
    return _ok(changeset=cs.id, note_ids=done, total_edits=len(p.edits),
               next="render_preview で聴く。戻すなら undo か reset_to_original")


@_tool
def set_fade(note_ids: list, fade_in_sec: float = None, fade_out_sec: float = None,
             author: str = "ai") -> dict:
    """ノートの**フェードイン／アウト**（画面の帯の上の角のつまみ。DAW のクリップフェードと同じ）。1 つの changeset。

    **音量だけ**を変える（ピッチ・なだらかさは変えない）。隣のノートは変えない（接続された境目にも付けられる）。
    fade_in_sec: ノートの頭から何秒で 0 → 元の音量にするか（編集後の秒。0 = 消す、省略 = そのまま）
    fade_out_sec: ノートの尻の何秒前から 0 へ下げるか（同上）
    形は等パワー（イン sin・アウト cos）。ノートの長さを変えてもフェードの秒は保ち、ノートより長くはならない
    （イン＋アウトがノートより長いと比を保って縮める）。フェードの外の音は元のサンプルのまま。
    list_notes / export_view_data の fade_in_sec / fade_out_sec に今の値が出る。消すのは両方 0。
    """
    from .project import fades as FD
    p = _project()
    p.reload_if_changed()
    ids = list(note_ids or [])
    by = {n.id: n for n in p.take_notes}
    missing = [i for i in ids if i not in by or by[i].kind != "note"]
    if missing:
        raise ProjectError("音程のあるノートが無い: %s（list_notes で確認）" % ", ".join(missing))
    if fade_in_sec is None and fade_out_sec is None:
        raise ProjectError("fade_in_sec か fade_out_sec を渡す（0 で消す）")
    try:
        rm, add, done = FD.fade_specs(p, ids, fade_in_sec, fade_out_sec)
    except ValueError as e:
        raise ProjectError(str(e))
    if not rm and not add:
        return _ok(changeset=None, note_ids=[], message="変わらなかった（同じ長さ）")
    clear = not add
    label = "フェードを消す" if clear else "フェード"
    cs = p.apply_changes(rm, add, author=author, label="%s（%d ノート）" % (label, len(done)))
    _rec(p, cs, label)
    now = FD.note_fades(p)
    return _ok(changeset=cs.id, note_ids=done, total_edits=len(p.edits),
               fades={i: [round(now[i][0], 4), round(now[i][1], 4)] if i in now else [0.0, 0.0]
                      for i in done},
               next="render_preview で聴く。戻すなら undo")


@_tool
def reset_to_original(note_ids: list = None, start_sec: float = None, end_sec: float = None,
                      author: str = "ai", boundary_ids: list = None) -> dict:
    """指定したノート／範囲を原音に戻す（1 つの changeset）。

    ピッチの編集は外す（鉛筆はこのノートにかかる部分だけ外す）。タイミングは**ノートの頭・尻を元の位置へ戻す**（接続された隣は
    伸び縮みで合わせる。後ろはずらさない）。戻した区間は原音のサンプルそのもの。
    ただし隣のノートのピッチを動かしたままなら、その境目のつなぎ（なだらかさ）は戻したノートの
    端にもかかる（段差にしないため。段差にしたいなら set_transition(value=0)）。
    無音にした（mute_notes）ノートは音が戻る（範囲の外の無音は残す）。
    フェード（set_fade）も外す（そのノートの頭のイン・尻のアウト）。

    boundary_ids: **音素の境目**（get_phonemes の boundaries[].id）を動かした編集（move_boundary）を
      外す（画面の音素の右クリック「子音｜母音の境目を元に戻す」）。ノートと一緒に渡してもよい
    """
    from .project import timing as TM
    p = _project()
    p.reload_if_changed()
    ids = []
    if note_ids:
        ids = [i for i in note_ids]
    if start_sec is not None or end_sec is not None:
        t0, t1 = _range(start_sec, end_sec)
        ids += [n.id for n in p.take_notes if n.end_sec > t0 and n.start_sec < t1
                and n.id not in ids]
    b_rm = []
    if boundary_ids:
        want = set(boundary_ids)
        b_rm = [e.id for e in p.edits if e.kind == "move_boundary"
                and e.params.get("boundary_id") in want]
    if not ids and not b_rm:
        return _ok(changeset=None, removed=0, message="対象が無かった")
    if not ids:
        cs = p.apply_changes(b_rm, [], author=author,
                             label="音素の境目を元に戻す（%d）" % len(b_rm))
        _rec(p, cs, "音素の境目を元に戻す")
        return _ok(changeset=cs.id, removed=len(b_rm), added=0, note_ids=[],
                   boundary_ids=list(boundary_ids), total_edits=len(p.edits))
    pitched = {n.id for n in TM.pitched_notes(p)}
    spans = {n.id: (n.start_sec, n.end_sec) for n in p.take_notes if n.id in ids}
    from .project import pitch as PI
    rm = []
    d_rm, d_add = PI.trim_draws(p, list(spans.values()))   # 鉛筆はこのノートにかかる部分だけ外す
    rm += d_rm
    p_rm, p_add = PI.trim_pitch_edits(p, list(spans.values()))
    rm += p_rm
    specs = list(d_add) + list(p_add)
    rm += b_rm
    m_rm, m_add = _trim_mutes(p, list(spans.values()))      # 無音にしたノートの音を戻す
    rm += m_rm
    from .project.fades import trim_fades
    rm += [i for i in trim_fades(p, list(spans.values())) if i not in rm]   # フェードも外す
    specs += m_add
    tids = [i for i in ids if i in pitched]
    if tids:
        plan = TM.plan_reset_timing(p, tids)
        r_ids, t_specs, _ = TM.realize(p, plan, 1.0)
        specs += t_specs
        rm += [i for i in r_ids if i not in rm]
    if not rm and not specs:
        return _ok(changeset=None, removed=0, message="対象の編集が無かった")
    cs = p.apply_changes(rm, specs, author=author, label="原音に戻す（%d ノート）" % len(ids))
    _rec(p, cs, "オリジナルに戻す")
    return _ok(changeset=cs.id, removed=len(rm), added=len(specs), note_ids=ids,
               total_edits=len(p.edits))


@_tool
def undo(changeset_id: str = None) -> dict:
    """直近の操作を取り消す（**曲で 1 本の履歴**。画面の Ctrl+Z と同じ）。

    取り消せるのは曲に保存されるものを変えた操作すべて: ノートの編集（ピッチ・長さ・移動・分割・結合・
    接続・なだらかさ・鉛筆・ガイドに合わせる・オリジナルに戻す・音素の境界）・歌詞・トラックの追加・
    外す・位置・名前・種類・ガイドの指定。ミュート／ソロ・表示・選択・編集対象の切り替えは対象外。
    **別のトラックの操作なら、そのトラックを編集対象にしてから戻す**（返り値の switched_to。
    切り替わったら analyze_take を呼ぶ）。画面と Claude Code の操作は同じ履歴に入る。

    changeset_id: 渡すと、編集対象のトラックのその changeset だけを取り消す（前の版と同じ。順は問わない。
      後の編集が対象にしているノートが無くなる取り消しはできない）
    """
    from . import mcp_tracks
    p = _project()
    if changeset_id is None and mcp_tracks.has_history():
        return _ok(**mcp_tracks.history_undo())
    cs = p.undo(changeset_id)
    mcp_tracks.mark_changeset(p, cs.id, undone=True)
    if any(o.get("op") == "lyrics" for o in cs.ops):
        _invalidate_renderer()
    return _ok(undone=cs.summary(), total_edits=len(p.edits), can_redo=p.can_redo())


@_tool
def redo() -> dict:
    """直近に取り消した操作をやり直す（曲で 1 本の履歴。画面の Ctrl+Shift+Z / Ctrl+Y と同じ）。
    別のトラックの操作なら、そのトラックを編集対象にしてからやり直す（返り値の switched_to）。"""
    from . import mcp_tracks
    p = _project()
    if mcp_tracks.has_history():
        return _ok(**mcp_tracks.history_redo())
    cs = p.redo()
    if any(o.get("op") == "lyrics" for o in cs.ops):
        _invalidate_renderer()
    return _ok(redone=cs.summary(), total_edits=len(p.edits), can_redo=p.can_redo())


# ---------------------------------------------------------------- 確かめる
@_tool
def render_preview(start_sec: float = None, end_sec: float = None, backend: str = "praat",
                   name: str = None, background: bool = None) -> dict:
    """編集を当てた音を WAV に書いてパスを返す。編集していない区間は原音のまま。

    backend: "praat"（既定。Praat の TD-PSOLA）/ "psola"（自前の TD-PSOLA）/ "world"。
        praat-parselmouth が無い環境では "praat" を頼んでも "psola" になる（返り値の backend が実際のもの）
    """
    p = _project()
    t0, t1 = _range(start_sec, end_sec)
    if background is None:
        background = (t1 - t0) * 1.5 > JOB_THRESHOLD_SEC

    def work():
        with _prep_yield():
            return _preview(p, backend, t0, t1, name)

    if background:
        return _submit_job("render_preview", work)
    return _ok(**work())


def _preview(p, backend, t0, t1, name):
    """render_preview の中身。"""
    r = _renderer(backend)
    segs = segments_for(p)
    y, info = r.render_range(t0, t1, segs)
    out = os.path.join(p.sub("renders"),
                       name or "preview-%s-%s.wav" % (time.strftime("%H%M%S"),
                                                      uuid.uuid4().hex[:5]))
    w = write_wav(out, y, p.take["sr"])
    return {"path": os.path.abspath(out), "range_sec": [round(t0, 3), round(t1, 3)],
            "backend": info["backend"], "edited_chunks": info["edited_chunks"],
            "chunks": info["chunks"], "out_sec": info["out_sec"],
            "warnings": info["warnings"], "peak": round(w["peak"], 4),
            "peak_normalized": w["normalized"],
            "note": "編集していない区間は原音のサンプルそのまま。つなぎ目は 20 ms クロスフェード"}


@_tool
def render_region(start_sec: float = None, end_sec: float = None, backend: str = "praat",
                  channels: str = "all", path: str = None) -> dict:
    """編集を当てた区間の PCM を WAV（32 bit float）に書いてパスを返す（DAW 連携向けの「区間 → PCM」）。

    `render_preview` との違い: **長さが変わらない**（[start, end) を頼めば end − start 秒ちょうど）、
    中身は `export_wav` が同じ範囲に書くものと同じ（編集のかたまりごとの窓を丸ごと再合成して切り出す）。
    DAW のクリップのキャッシュを区間ごとに作り直す用途。
    返り値の `source_start_sec` がソース上の開始位置、`timing_sec` が掛かった秒。

    channels: "all"（既定。素材のチャンネルそのまま）/ "mono"（解析と同じモノラル）
    """
    p = _project()
    p.reload_if_changed()                   # 画面など外で足された編集も当てる
    t0, t1 = _range(start_sec, end_sec)
    from .render.region import RegionRenderer, render_region as _rr
    key = (resolve_backend_name(backend), channels)
    rr = _state["region"].get(key)
    if rr is None:
        rr = RegionRenderer.for_project(p, backend=backend, channels=channels)
        _state["region"] = {key: rr}
    with _prep_yield():
        y, info = _rr(p, t0, t1, renderer=rr)
    out = path or os.path.join(p.sub("renders"), "region-%s-%s.wav"
                               % (time.strftime("%H%M%S"), uuid.uuid4().hex[:5]))
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    import soundfile as sf
    sf.write(out, y.astype("float32"), info["sr"], subtype="FLOAT")
    return _ok(path=out, **info)


@_tool
def render_audition(note_id: str, cents: float = 0.0, start_sec: float = None,
                    end_sec: float = None, backend: str = "praat") -> dict:
    """**画面向け**: つかんだノートのプレビュー音（ノートをドラッグしている間に鳴らす。issue #27）。

    ノートを `cents` だけ動かした**つもり**で [start_sec, end_sec)（既定はノートの範囲）を再合成し、
    モノラル 32 bit float の WAV（プロジェクトの renders/audition.wav。毎回上書き）のパスを返す。
    **プロジェクトは書き換えない**（編集も取り消しの履歴も増えない）。中身は、確定した編集に
    `shift_pitch(cents, note_id)` を足したときの `render_region` と同じ。LLM が聴くなら render_preview を使う。
    """
    p = _project()
    p.reload_if_changed()
    from .render.region import RegionRenderer, audition as _aud
    name = resolve_backend_name(backend)
    # 再生（render_tracks）が作った下ごしらえがあれば使い回す（チャンネルは問わない）。無ければモノラルで作る
    rr = _state["region"].get((name, "mono")) or _state["region"].get((name, "all"))
    if rr is None:
        rr = RegionRenderer.for_project(p, backend=backend, channels="mono")
        _state["region"][(name, "mono")] = rr
    with _prep_yield():
        y, info = _aud(p, note_id, cents, start_sec, end_sec, renderer=rr)
    if y.ndim == 2 and y.shape[1] > 1:
        y = y.mean(axis=1)
    out = os.path.abspath(os.path.join(p.sub("renders"), "audition.wav"))
    import soundfile as sf
    tmp = out + ".tmp.wav"
    sf.write(tmp, np.asarray(y, dtype="float32").reshape(-1), info["sr"], subtype="FLOAT")
    os.replace(tmp, out)
    return _ok(path=out, sr=info["sr"], frames=info["frames"], start_sec=info["start_sec"],
               note_id=note_id, cents=info["cents"], backend=info["backend"],
               rendered_windows_sec=info["rendered_windows_sec"], timing_sec=info["timing_sec"])


@_tool
def export_wav(path: str = None, start_sec: float = None, end_sec: float = None,
               backend: str = "praat", background: bool = None,
               full_source: bool = False) -> dict:
    """編集を当てた WAV を書き出す。**元と同じ長さ・開始位置**で、編集区間だけ差し替える。

    `render_preview` との違い: こちらは **DAW に戻すファイル**なので、
    サンプルレート・ビット深度・チャンネル数・総サンプル数・開始位置が元と同じになる
    （曲頭 0:00 起点のまま。トラックに置き直せば位置が合う）。
    **編集していないところは元のファイルとビット単位で同じ。**

    path: 書き先。省略時は**元ファイルの隣**の `<take名>_ve.wav`（既存のファイルは上書きせず
        `_ve(2)`、`(3)` … にする。元がサンプル列・元の場所に書けないときは `<project>/export/`）
    start_sec / end_sec: この範囲の編集だけを反映する（省略時は全部）。
        **範囲を絞っても出力は素材まるごと**（長さは変わらない）
    backend: "praat"（既定。Praat の TD-PSOLA）/ "psola"（自前の TD-PSOLA）/ "world"。
        praat-parselmouth が無い環境では "praat" を頼んでも "psola" になる（返り値の backend が実際のもの）
    full_source: テイクがファイルの一部（open_project の offset_sec / length_sec）のとき、
        False（既定）ならクリップの長さの WAV（`start_sec` = ソース上の開始秒）、
        True なら**ソースと同じ長さ**でクリップの範囲だけ差し替えた WAV

    元の WAV の BWF（`bext` の TimeReference＝DAW 上の位置、`iXML`）を書き写す（返り値の `bwf`）。
    クリップをクリップの長さで書き出すときは TimeReference をクリップの開始ぶん進める
    （元に bext が無くても、クリップなら TimeReference = 開始位置の bext を足す）。
    **トラックの位置をずらしていれば（set_track の offset_sec）、TimeReference もその量だけ動かす**
    （中身・長さは元のまま。DAW で「元の位置へ」を押すと、ずらした位置に来る。返り値の `timeline_offset_sec`）。

    返り値の `replaced_spans_sec` が実際に差し替えた区間。その外は元のサンプルのまま。
    """
    p = _project()
    from .render.export import export_wav as _export
    if background is None:
        background = p.duration_sec > 60.0

    from .mcp_tracks import current_offset_sec
    shift = current_offset_sec()

    def work(cancel=None, report=None, commit=None):
        with _prep_yield():
            r = _export(p, path=path, start_sec=start_sec, end_sec=end_sec, backend=backend,
                        full_source=full_source, position_shift_sec=shift,
                        cancel=cancel, progress=report, commit=commit)
        r["timeline_offset_sec"] = round(shift, 6)
        return r

    if background:
        return _submit_job("export_wav", work, cancellable=True)
    return _ok(**work())


@_tool
def render_view(start_sec: float = None, end_sec: float = None, show_guide: bool = True,
                title: str = None) -> dict:
    """ピアノロール PNG を書いてパスを返す（画像は結果に入れない）。"""
    p = _project()
    t0, t1 = _range(start_sec, end_sec)
    return _ok(**_render_view(p, t0, t1, show_guide=show_guide, title=title))


@_tool
def export_view_data(start_sec: float = None, end_sec: float = None,
                     peak_ms: float = None, path: str = None) -> dict:
    """**画面（UI）向け**の描画データを JSON ファイルに書いてパスを返す。

    LLM 向けではない。エージェントがこれを読む必要はない（要約が欲しいときは
    list_notes / get_pitch / list_deviations を使うこと）。生の配列は MCP の結果に
    載せず、**すべて JSON ファイルの中**に置く。

    peak_ms を省くと範囲の長さから決める（曲全体で JSON が太らないように）。
    中身: 波形のピーク列（peak_ms ごとの min/max）、テイク F0（10 ms、編集前と編集後）、
    ガイド F0（ガイドの時刻 + 全体のずれでテイク時間に置いたもの）、ノート一覧（id・開始/終了・音名・
    編集前後の中心ピッチ・ピッチ編集対象外フラグ）、境界一覧（編集前後の秒）、
    編集前→編集後の時間写像、編集リストと取り消し履歴。
    """
    p = _project()
    p.reload_if_changed()
    return _ok(**_export_view_data(p, start_sec, end_sec, peak_ms=peak_ms, path=path))


@_tool
def remeasure(start_sec: float = None, end_sec: float = None, backend: str = "praat",
              keep_wav: bool = False, background: bool = None) -> dict:
    """編集後の音を測り直す。ずれがどれだけ減ったかを返す（AI が自分で検算するため）。"""
    p = _project()
    t0, t1 = _range(start_sec, end_sec)
    if background is None:
        background = (t1 - t0) * 2.0 > JOB_THRESHOLD_SEC

    def work():
        from .analysis.notes import segment_notes
        r = _renderer(backend)
        segs = segments_for(p)
        y, info = r.render_range(t0, t1, segs)
        sr = p.take["sr"]
        out = os.path.join(p.sub("renders"), "remeasure-%s.wav" % uuid.uuid4().hex[:6])
        write_wav(out, y, sr)
        f0new = f0mod.estimate_f0(x=y, sr=sr)
        before = f0mod.summarize(p.take_f0.f0, p.take_f0.voiced, p.take_f0.confidence,
                                 int(t0 / p.take_f0.hop_s), int(t1 / p.take_f0.hop_s))
        after = f0mod.summarize(f0new.f0, f0new.voiced, f0new.confidence)
        res = {"range_sec": [round(t0, 3), round(t1, 3)], "backend": info["backend"],
               "before": before, "after": after, "wav": os.path.abspath(out),
               "edited_chunks": info["edited_chunks"], "warnings": info["warnings"]}
        if p.guide is not None and p.alignment is not None:
            from .analysis.align import deviations as _devs
            new_notes = segment_notes(f0new, source="take", id_prefix="r")
            # 出力の時刻は t0 だけずれているので、ガイド側の写像に合わせて戻す
            for n in new_notes:
                n.start_sec = round(n.start_sec + t0, 4)
                n.end_sec = round(n.end_sec + t0, 4)
            g2t, basis = p.guide_to_take()
            dtw = basis["basis"] == "dtw"
            devs_new, _ = _devs(new_notes, _shifted(f0new, t0, p.take_f0.hop_s),
                                p.guide_notes, p.guide_f0, p.alignment,
                                guide_to_take=None if dtw else g2t,
                                offset_sec=None if dtw else basis["offset_ms"] / 1000.0)
            devs_old, _ = p.deviations()
            res["deviation_summary"] = {
                "before": _dev_stats([d for d in devs_old if d.end_sec > t0 and d.start_sec < t1]),
                "after": _dev_stats(devs_new),
                "note": "after は編集後の音を測り直してガイドと突き合わせた値",
            }
        if not keep_wav:
            try:
                os.remove(out)
                res["wav"] = None
            except OSError:
                pass
        return res

    if background:
        return _submit_job("remeasure", work)
    return _ok(**work())


def _shifted(f0r, t0, hop):
    """t0 秒ぶん前に 0 を詰めた F0Result（絶対時刻に揃えるため）。"""
    n = int(round(t0 / hop))
    if n <= 0:
        return f0r
    import copy
    g = copy.copy(f0r)
    g.f0 = np.concatenate([np.zeros(n), f0r.f0])
    g.voiced = np.concatenate([np.zeros(n, dtype=bool), f0r.voiced])
    g.confidence = np.concatenate([np.zeros(n), f0r.confidence])
    g.rms_db = np.concatenate([np.full(n, -120.0), f0r.rms_db])
    return g


def _dev_stats(devs):
    pc = [abs(d.pitch_cents) for d in devs if d.pitch_cents is not None]
    tm = [abs(d.timing_ms) for d in devs if d.timing_ms is not None]
    return {
        "n_notes": len(devs),
        "abs_pitch_cents_median": round(float(np.median(pc)), 1) if pc else None,
        "abs_pitch_cents_max": round(float(np.max(pc)), 1) if pc else None,
        "over_50_cents": int(sum(1 for v in pc if v > 50)),
        "abs_timing_ms_median": round(float(np.median(tm)), 1) if tm else None,
        "abs_timing_ms_max": round(float(np.max(tm)), 1) if tm else None,
    }


@_tool
def list_changes(include_undone: bool = False) -> dict:
    """編集リストと changeset の一覧。"""
    p = _project()
    p.reload_if_changed()
    cs = [c.summary() for c in p.changesets if include_undone or not c.undone]
    return _ok(changesets=cs, edits=[dict(e.to_json(), describe=e.describe()) for e in p.edits],
               total_edits=len(p.edits), can_redo=p.can_redo())


@_tool(lock=False)
def get_job(job_id: str) -> dict:
    """長い処理の結果を取りに行く。"""
    with _jobs_lock:
        j = _jobs.get(job_id)
        if j is not None:
            j = j.copy()
    if j is None:
        return {"ok": False, "error": "そんな job_id は無い: %s" % job_id}
    out = {"ok": True, "job_id": job_id, "status": j["status"], "name": j["name"],
           "elapsed_sec": round((j.get("finished_at") or time.time()) - j["started_at"], 2),
           "progress": j["progress"], "cancellable": j["cancellable"]}
    if j.get("stage"):
        from .prep import STAGE_LABELS
        out["stage"] = j["stage"]
        out["stage_label"] = STAGE_LABELS.get(j["stage"], j["stage"])
    out.update(j.get("info") or {})
    if j["status"] == "done":
        out.update(j["result"])
    elif j["status"] == "error":
        out["ok"] = False
        out["error"] = j["error"]
    return out


@_tool(lock=False)
def cancel_job(job_id: str) -> dict:
    """中断できるジョブへ要求を送り、安全な境界で処理を止める。"""
    with _jobs_lock:
        j = _jobs.get(job_id)
        if j is None:
            return {"ok": False, "error": "そんな job_id は無い: %s" % job_id}
        if not j["cancellable"] or j["status"] != "running":
            return {"ok": False, "error": "このジョブは取り消せない"}
        j["cancel"].set()
        return {"ok": True, "job_id": job_id, "status": "canceling"}


@_tool(lock=False)
def prep_status() -> dict:
    """**画面向け**: トラックごとの裏の準備（issue #63）の状態。エンジンのロックを握らないので、
    ほかの処理の最中でもすぐ返る。

    tracks[]: {id, state, stage, stage_label, progress, error, paused}
      state: "ready"（準備済み。選べばすぐ開く）/ "preparing"（準備中。stage が今の段）/
             "queued"（待ち）/ "failed"（失敗。error。選んだときに表でもう一度解析する）
      paused: 表の重い処理・一時停止の間、次の段へ進まずに待っている
    running: 今準備しているトラックの id（無ければ null）。
    """
    from . import prep
    return _ok(**prep.overview())


@_tool(lock=False)
def pause_prep(paused: bool = True) -> dict:
    """裏の準備を一時停止する／再開する（走っている段は最後まで進み、次の段の前で止まる）。
    analyze_take が合流して待っているトラックは止めない。"""
    from . import prep
    prep.set_paused(paused)
    return _ok(**prep.overview())


@_tool(lock=False)
def engine_info(reload_addons: bool = False) -> dict:
    """エンジンの状態（バージョン・バックエンド・重みの有無・アドオン・ログの場所）。

    reload_addons: True ならアドオンの置き場を見直す（画面がアドオンを取得・削除した直後に呼ぶ。再起動しなくてよい）。
    """
    from . import addons
    from .analysis.align import DEFAULT_METHOD
    from .analysis.phonemes import backend_info
    from .render.base import list_backends
    if reload_addons:
        addons.activate()
    p = _project(required=False)
    return _ok(version=__version__, backends=list_backends(),
               rmvpe_model=f0mod.RMVPE_PATH,
               rmvpe_model_found=os.path.exists(f0mod.RMVPE_PATH),
               models_dir=f0mod.DEFAULT_MODELS_DIR,
               project=(p.dir if p else None), log=log.current_log_file(),
               phonemes_supported=True, phonemes=backend_info(),
               dtw_method=DEFAULT_METHOD, addons=addons.summary(),
               lyrics=(p.lyrics or None) if p else None,
               limits={"job_threshold_sec": JOB_THRESHOLD_SEC,
                       "min_phoneme_ms": 20.0})


TOOLS = [open_project, set_lyrics, get_lyrics, list_utterances, set_note_syllable,
         inspect_lyrics_score, import_lyrics, analyze_take, get_pitch, list_notes, list_deviations,
         get_phonemes,
         shift_pitch, set_pitch_curve, move_note, stretch, move_boundary, correct_to_guide,
         set_transition, split_note, merge_notes,
         plan_edit, apply_plan, list_connections, set_connection,
         mute_notes, set_fade, reset_to_original, undo, redo,
         render_preview, render_region, render_audition, render_view, export_wav,
         export_view_data, remeasure,
         list_changes,
         get_job, cancel_job, prep_status, pause_prep, engine_info]

# トラック（セッション。issue #7）。mcp_tracks はこのモジュールの _tool などを使うので最後に読む
from . import mcp_tracks as _mcp_tracks   # noqa: E402
TOOLS += _mcp_tracks.TOOLS
# プロジェクトのファイル（新規・開く・保存。issue #33）
from . import mcp_document as _mcp_document   # noqa: E402
TOOLS += _mcp_document.TOOLS
# 区間の音声認識で歌詞を確かめる（issue #54）
from . import mcp_asr as _mcp_asr   # noqa: E402
TOOLS += _mcp_asr.TOOLS


def build_server():
    from mcp.server.mcpserver import MCPServer
    server = MCPServer(
        name=SERVER_NAME, version=__version__,
        instructions=(
            "Gliss（歌声のピッチ・タイミング編集）のエンジン。測る（analyze_take / list_notes / get_phonemes / "
            "list_deviations / get_pitch）→ "
            "直す（shift_pitch / set_pitch_curve / move_note / stretch / move_boundary / "
            "correct_to_guide / split_note / merge_notes / set_transition）→ "
            "（タイミングの編集は**後ろをずらさない**。隣との接続 / 切り離しは list_connections）→ "
            "確かめる（render_preview / render_view / remeasure）の順で使う。"
            "**Gliss の画面で開いている曲を触るときは、最初に load_project を引数なしで呼ぶ**"
            "（画面の曲と編集中のトラックを開く。編集は画面に即反映される）。"
            "ファイルから始めるときは open_project / load_project(path)。"
            "画面の「AI に許可」で許していない操作（編集／保存・書き出し）は ok=false が返る。"
            "**歌詞が分かっているなら set_lyrics を呼ぶ**と"
            "音素アラインメントが走り、タイミング編集の単位が音素境界になる"
            "（母音だけ伸縮して子音の長さを保てる）。"
            "編集は非破壊で、undo で changeset 単位に戻せる。"
            "歌詞を耳で確かめるには transcribe(start_sec, end_sec) で区間を聞き取る（結果は候補。"
            "確定の歌詞は変わらないので、取り込むなら set_lyrics / set_note_syllable を呼ぶ）。"
            "画像と生の数値列は結果に入れない（パスを返すので Read で開く）。"),
    )
    for fn in TOOLS:
        server.add_tool(fn, name=fn.__name__,
                        description=(fn.__doc__ or "").strip() or fn.__name__)
    return server


def main():
    # stdout は mcp 2.x の stdio サーバーが自分で退避させる（log.guard_stdout 参照）
    from .project.document import cleanup_pending
    cleanup_pending()
    # 任意機能のアドオン（配布版の exe に無い依存。addons.py）を sys.path に足す
    from . import addons
    try:
        for a in addons.activate():
            log.get().info("アドオン %s: %s", a["id"], "読む" if a["active"] else (a["source"] or a["reason"]))
    except Exception:                       # noqa: BLE001  アドオンが壊れていてもエンジンは起動する
        log.get().exception("アドオンを読めなかった")
    log.get().info("=== vocal_engine MCP サーバー起動（%s / pid %d）", __version__, os.getpid())
    server = build_server()
    try:
        server.run(transport="stdio")
    finally:
        log.get().info("=== 終了")


if __name__ == "__main__":
    main()
