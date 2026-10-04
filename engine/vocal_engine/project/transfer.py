# -*- coding: utf-8 -*-
"""`.gliss`（単体のプロジェクト）のトラックの編集を、DAW（ARA）の文書へ移すための変換（読むだけ）。

`.gliss` のトラックの `take`・`lyrics`・`changesets` を `Project.to_archive()` と同じ形（ARA のアーカイブ）に並べ替える。
編集の秒はソース（素材）の秒でトラックの位置に依らないので、**同じ素材**の ARA の修飾へそのまま当てられる
（`mcp_ara.import_edits`・`ara_restore`）。素材は `clip_audio_sha256`（音の中身のハッシュ）で照らす。

移るもの: テイクの編集（changeset の列 = 取り消し履歴・author）・歌詞・F0 の方式（`f0_estimator`）。
移らないもの: セッションの項目（`mutes`・`cuts`・ゲイン・パン・ミュート・ソロ・ガイドの指定・テンポ）と、
クリップ（素材の一部）のトラック。`archive_from_gliss` の返り値の `not_transferred` に一覧を返す。
"""
import copy
import os

from .. import media as M
from . import document as D
from .store import ARCHIVE_FORMAT, ARCHIVE_VERSION, SCHEMA_VERSION, ProjectError

# params の a / b がノート ID の編集（解析の方式が違うと別のノートになる）
ID_PARAM_KINDS = ("connection", "transition", "merge")
_REF_KEYS = ("source_id", "source_kind", "source_name", "sha256", "sr", "channels",
             "subtype", "source_frames", "offset_frames", "frames", "path", "pad")


def effective_edits(changesets):
    """changeset の列（アーカイブの `changesets`）を順に当てた、有効な編集（dict）の列。
    `Project._replay` と同じ規則（取り消した changeset は飛ばす・`in_place_of` は元の位置）。"""
    by_id, order = {}, []
    for cs in changesets or []:
        if cs.get("undone"):
            continue
        for op in cs.get("ops") or []:
            if op.get("op") == "add":
                e = op["edit"]
                by_id[e["id"]] = e
                at = op.get("in_place_of")
                if at and at in order:
                    order.insert(order.index(at), e["id"])
                else:
                    order.append(e["id"])
            elif op.get("op") == "remove":
                by_id.pop(op.get("edit_id"), None)
    seen, out = set(), []
    for eid in order:
        if eid in by_id and eid not in seen:
            seen.add(eid)
            out.append(by_id[eid])
    return out


def is_note_dependent(edit):
    """編集がノート ID に頼るか（`target` が note、または connection・transition・merge の `params.a/b`）。"""
    return (edit.get("target") or {}).get("type") == "note" or edit.get("kind") in ID_PARAM_KINDS


def edit_stats(archive):
    """アーカイブの編集の要約: edits（有効な編集の数）・changesets・undone（取り消し済みの changeset）・
    authors（{human, ai}）・kinds・note_targets（ノート ID に頼る編集の数）。"""
    css = archive.get("changesets") or []
    edits = effective_edits(css)
    authors, kinds = {}, {}
    for e in edits:
        authors[e.get("author", "ai")] = authors.get(e.get("author", "ai"), 0) + 1
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    return {"edits": len(edits), "changesets": len(css), "undone": sum(1 for c in css if c.get("undone")),
            "authors": authors, "kinds": kinds, "note_targets": sum(1 for e in edits if is_note_dependent(e)),
            "changeset_authors": sorted({c.get("author", "ai") for c in css})}


def _pick_track(doc, track):
    """`.gliss` の文書から編集の対象のトラックを選ぶ（id か名前。省くとボーカルのトラックが 1 本ならそれ）。"""
    tracks = [t for t in (doc.get("session") or {}).get("tracks") or []]
    vocal = [t for t in tracks if t.get("kind") == "vocal"]
    if track:
        hit = [t for t in tracks if t.get("id") == track] or [t for t in tracks if t.get("name") == track]
        if len(hit) != 1:
            raise ProjectError("トラックが見つからない・または複数ある: %s（%s）" % (
                track, ", ".join("%s=%s" % (t.get("id"), t.get("name")) for t in vocal)))
        t = hit[0]
    elif len(vocal) == 1:
        t = vocal[0]
    else:
        raise ProjectError("トラックを指定する（track: id か名前）。ボーカルのトラック: %s" % (
            ", ".join("%s=%s" % (t.get("id"), t.get("name")) for t in vocal) or "なし"))
    if t.get("kind") != "vocal":
        raise ProjectError("伴奏のトラックには編集が無い: %s" % t.get("name"))
    return t


def _not_transferred(t, doc):
    """移らないセッションの項目の一覧（トラックにあるものだけ）。"""
    out = []
    mutes = [list(r) for r in (t.get("mutes") or [])]
    if mutes:
        out.append({"item": "mutes", "count": len(mutes), "ranges_sec": mutes,
                    "note": "mute_track_range の区間（セッションの項目）。DAW でその区間をミュートするか、mute_notes で作り直す"})
    cuts = [list(r) for r in (t.get("cuts") or [])]
    if cuts:
        out.append({"item": "cuts", "count": len(cuts), "ranges_sec": cuts,
                    "note": "split_track の切れ目（セッションの項目）。DAW のイベントの分割で作り直す"})
    mix = {k: t.get(k) for k, dflt in (("gain_db", 0.0), ("pan", 0.0), ("mute", False), ("solo", False))
           if t.get(k) not in (None, dflt)}
    if mix:
        out.append({"item": "mix", "values": mix, "note": "ゲイン・パン・ミュート・ソロは DAW のミキサーの項目"})
    if t.get("offset_sec"):
        out.append({"item": "offset_sec", "value": t["offset_sec"],
                    "note": "トラックの位置。編集は素材の秒なので位置に依らない（DAW のイベントの位置で決まる）"})
    if (doc.get("session") or {}).get("guide"):
        out.append({"item": "guide", "note": "ガイドの指定。ARA の文書では DAW の別の修飾（set_guide_track）。"
                                              "編集は計算済みの値なので取り込みには要らない"})
    return out


def archive_from_gliss(gliss_path, track=None, estimator=None):
    """`.gliss` のトラックを ARA のアーカイブ（`Project.to_archive()` の形）にする。

    gliss_path: `.gliss` のファイル（保存した中身を読む。画面で開いていて未保存の編集は入らない）。
    track: トラックの id か名前（省くとボーカルが 1 本ならそれ）。
    estimator: F0 の方式を指定する（省くと、トラックに明示した方式。記録が無ければ None）。
    返り値: {archive, track, material, estimator, stats, not_transferred, warnings}。
    素材（音声ファイル）が見つからない・クリップ（素材の一部）のトラックは ProjectError。"""
    gliss_path = os.path.abspath(str(gliss_path))
    if not os.path.isfile(gliss_path):
        raise ProjectError(".gliss が見つからない: %s" % gliss_path)
    doc = D.read_file(gliss_path)
    t = _pick_track(doc, track)
    pj = (doc.get("projects") or {}).get(t.get("project_dir"))
    if pj is None:
        raise ProjectError("トラックの編集が .gliss に無い: %s" % t.get("name"))
    take = copy.deepcopy(pj.get("take"))
    if not take:
        raise ProjectError("トラックにテイクの参照が無い: %s" % t.get("name"))
    source_frames = int(take.get("source_frames") or take.get("frames") or 0)
    if t.get("clip") or int(take.get("offset_frames") or 0) != 0 or int(take.get("frames") or 0) != source_frames:
        raise ProjectError("クリップ（素材の一部）のトラックは移せない（ARA の文書は素材の全体を 1 つの修飾にする）: %s"
                           % t.get("name"))
    # 音声の場所（絶対 → 相対 → .gliss と同じフォルダ）を今の場所に直す
    mini = {"take": take}
    D._resolve(mini, os.path.dirname(gliss_path))
    take = mini["take"]
    if not os.path.exists(take["path"]):
        raise ProjectError("トラックの音声が見つからない（素材の識別に音の中身を読む）: %s" % take["path"])
    ref = {k: take.get(k) for k in _REF_KEYS}
    ref["clip_audio_sha256"] = M.clip_audio_hash(take)
    est = estimator or t.get("estimator") or None
    if est is not None:
        from ..analysis import f0 as f0mod
        if est not in f0mod.ESTIMATORS:
            raise ProjectError("estimator は %s のどれか（%r は知らない）" % (" / ".join(f0mod.ESTIMATORS), est))
    archive = {
        "format": ARCHIVE_FORMAT,
        "version": ARCHIVE_VERSION,
        "engine_schema": pj.get("schema_version") or SCHEMA_VERSION,
        "take": ref,
        "guide": None,
        "lyrics": {k: copy.deepcopy(v) for k, v in (pj.get("lyrics") or {}).items() if k != "guide"},
        "auto_lyrics_attempted": True,          # 歌詞は .gliss のものを引き継ぐ（取り込み先で聞き取りをやり直さない）
        "align_method": pj.get("align_method") or "dtw",
        "f0_estimator": est,
        "seq": dict(pj.get("seq") or {"edit": 0, "changeset": 0}),
        "changesets": copy.deepcopy(pj.get("changesets") or []),
    }
    stats = edit_stats(archive)
    not_transferred = _not_transferred(t, doc)
    warnings = []
    if est is None:
        warnings.append("F0 の方式の記録が無い。補正を作った方式と違うと再合成の音が変わる（取り込み先で方式を指定する。"
                        "ノートの ID に頼る編集が %d 件ある）" % stats["note_targets"] if stats["note_targets"] else
                        "F0 の方式の記録が無い。補正を作った方式と違うと再合成の音が変わる（取り込み先で方式を指定する）")
    elif stats["note_targets"]:
        warnings.append("ノートの ID に頼る編集が %d 件ある。取り込み先の解析は %s にそろえる（方式が違うとノートの番号が変わる）"
                        % (stats["note_targets"], est))
    for n in not_transferred:
        if n["item"] in ("mutes", "cuts"):
            warnings.append("%s %d 件は移らない" % (n["item"], n["count"]))
    return {
        "archive": archive,
        "track": {"id": t.get("id"), "name": t.get("name"), "sr": t.get("sr"), "channels": t.get("channels"),
                  "source_frames": source_frames, "duration_sec": t.get("duration_sec"), "gliss": gliss_path},
        "material": {"name": take.get("source_name") or os.path.basename(take["path"]), "path": take["path"],
                     "frames": ref["frames"], "sr": ref["sr"], "channels": ref["channels"],
                     "sha256": ref["sha256"], "clip_audio_sha256": ref["clip_audio_sha256"]},
        "estimator": est,
        "stats": stats,
        "not_transferred": not_transferred,
        "warnings": warnings,
    }
