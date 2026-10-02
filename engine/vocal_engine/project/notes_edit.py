# -*- coding: utf-8 -*-
"""ノートの**分割**と**結合**（カットモード。Melodyne の分割ツールと同じ）。

解析で出たノート（`cache/take-analysis.json`）はそのままにして、編集リストの
`split` / `merge` を**時刻で**上に重ねる（`Project.take_notes` がこれを通したノートを返す）。

- `split`（target = [t, t]）: t を中に含むノート・子音・息・無音を 2 つに分ける。左は元の id のまま、
  右は `<元の id>@<t のミリ秒>`（例 `n005@1234`）。id が時刻から決まるので、
  分割の順番を変えても・解析し直しても同じ id になる
- `merge`（target = [t, t]）: 尻と頭が t で接する同じ種類の区間を 1 つにする（左の id）

**時刻で持つので、解析をやり直しても（ノートの id が振り直されても）ユーザーの分割は残る**。
その時刻を含むノートが無くなったとき（無声になった等）は飛ばしてログに書く。

分割・結合するときは、同じ changeset の中で次を書き換えて**音を変えない**:

- そのノートを対象にした編集（`pitch_shift` など、target = note）→ 元の区間の範囲対象に
  （分割で片方の id が変わっても、結合で id が消えても、同じ区間に同じ量が掛かる）
- 接続 / つなぎのなだらかさ（`connection` / `transition`、params に隣の id を持つ）→
  新しい id の組に付け替える（結合で消える境目の `connection` は外す。`transition` は**時刻で**
  段差に掛かるので残す = 結合しても線と音が変わらない。`project/pitch.py`）
- 結合: 境目にあった無音の挿入（`silence`。切り離して縮めた隙間）を外し、結合したノートの頭と尻の
  位置はそのままにノート全体を伸縮して埋める（ノートの中に無音の穴を残さない。後ろはずらさない）。
  片側だけにある `crop` / `silence`（範囲）はそのまま残す
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from .. import log
from ..analysis.f0 import hz_to_midi, midi_to_name
from ..analysis.notes import SUNG_MAX_IQR_ST, SUNG_MIN_MS
from .model import Target

MIN_PIECE_SEC = 0.02          # 分割後のノートの最短（timing.MIN_BODY_SEC と同じ）
MERGE_TOL_SEC = 0.006         # 「接している」とみなす隙間
PAIR_KINDS = ("connection", "transition")


class NoteEditError(RuntimeError):
    pass


def right_id(note_id, t):
    root = str(note_id).split("@")[0]
    return "%s@%d" % (root, int(round(float(t) * 1000.0)))


def piece(n, s, e, nid, f0r):
    """ノート n の [s, e) を 1 つのノートにする（音程などは F0 から測り直す）。"""
    s, e = round(float(s), 4), round(float(e), 4)
    hop = f0r.hop_s
    fs = max(0, int(round(s / hop)))
    fe = min(len(f0r.f0), max(fs + 1, int(round(e / hop))))
    f0 = np.asarray(f0r.f0[fs:fe], dtype="float64")
    v = np.asarray(f0r.voiced[fs:fe], dtype=bool) & (f0 > 0)
    fv = f0[v]
    rms = np.asarray(f0r.rms_db[fs:fe], dtype="float64")
    kw = {}
    if len(fv) and n.kind == "note":
        midi = hz_to_midi(fv)
        q1, q3 = np.percentile(midi, [25, 75])
        iqr = float(q3 - q1)
        med = float(np.median(midi))
        dur_ms = (e - s) * 1000.0
        label = ("sung" if (dur_ms >= SUNG_MIN_MS and iqr <= SUNG_MAX_IQR_ST)
                 else ("short" if dur_ms < SUNG_MIN_MS else "voiced_unstable"))
        kw = dict(pitch_hz=round(float(np.median(fv)), 3), pitch_midi=round(med, 3),
                  note_name=midi_to_name(med), iqr_semitones=round(iqr, 3), label=label)
    return replace(n, id=nid, start_sec=s, end_sec=e, start_frame=int(fs), end_frame=int(fe),
                   rms_peak_db=round(float(np.max(rms)), 2) if len(rms) else n.rms_peak_db,
                   **kw)


def _find_split(ns, t):
    for i, n in enumerate(ns):
        if n.kind in ("note", "unvoiced", "breath", "silence") and n.start_sec + MIN_PIECE_SEC - 1e-9 <= t <= n.end_sec - MIN_PIECE_SEC + 1e-9:
            return i
    return None


def _find_merge(ns, t):
    for i in range(len(ns) - 1):
        a, b = ns[i], ns[i + 1]
        if (a.kind == b.kind and a.kind in ("note", "unvoiced", "breath", "silence") and abs(a.end_sec - t) <= MERGE_TOL_SEC
                and abs(b.start_sec - t) <= MERGE_TOL_SEC):
            return i
    return None


def apply_note_edits(notes, ops, f0r):
    """解析のノート列に `split` / `merge` を編集リストの順に当てる。"""
    ns = list(notes)
    for e in ops:
        t = float(e.target.start_sec)
        if e.kind == "split":
            i = _find_split(ns, t)
            if i is None:
                log.get().warning("分割 %s: %.4f s を含むノートが無いので飛ばす", e.id, t)
                continue
            n = ns[i]
            ns[i:i + 1] = [piece(n, n.start_sec, t, n.id, f0r),
                           piece(n, t, n.end_sec, right_id(n.id, t), f0r)]
        elif e.kind == "merge":
            i = _find_merge(ns, t)
            if i is None:
                log.get().warning("結合 %s: %.4f s で接するノートが無いので飛ばす", e.id, t)
                continue
            a, b = ns[i], ns[i + 1]
            ns[i:i + 2] = [piece(a, a.start_sec, b.end_sec, a.id, f0r)]
    return ns


# ---------------------------------------------------------------- 編集を作る
def _retarget(project, note_ids):
    """そのノートを対象にした編集 → 元の区間の範囲対象（音は変わらない）。(外す id, 入れる spec)"""
    spans = {}
    for nid in note_ids:
        n = project.note(nid)
        spans[nid] = (n.start_sec, n.end_sec)
    rm, add = [], []
    for e in project.edits:
        if e.target.type == "note" and e.target.note_id in spans:
            a, b = spans[e.target.note_id]
            rm.append(e.id)
            add.append({"kind": e.kind, "target": Target.range(a, b), "params": dict(e.params),
                        "note": e.note, "in_place_of": e.id})
    return rm, add


def _repair_pairs(project, rename, drop):
    """`connection` / `transition` を新しい id の組へ。rename = {旧 id: 新 id}（左側 a だけ）、
    drop = 外す組の集合（`connection` だけ外す。`transition` は時刻で引くので残す）。"""
    rm, add = [], []
    for e in project.edits:
        if e.kind not in PAIR_KINDS:
            continue
        a, b = e.params.get("a"), e.params.get("b")
        if (a, b) in drop:
            if e.kind == "connection":
                rm.append(e.id)
        elif a in rename:
            rm.append(e.id)
            add.append({"kind": e.kind, "target": e.target,
                        "params": dict(e.params, a=rename[a]), "note": e.note,
                        "in_place_of": e.id})
    return rm, add


def snap_time(project, t, snap_sec):
    """音素境界が snap_sec 以内にあればそこへ寄せる。(t, 寄せたか)"""
    if not snap_sec or snap_sec <= 0 or not project.has_lyrics("take"):
        return float(t), False
    r = project.phonemes("take")
    if r is None or not r.boundaries:
        return float(t), False
    best = min(r.boundaries, key=lambda b: abs(b.time_sec - t))
    if abs(best.time_sec - t) <= snap_sec:
        return float(best.time_sec), True
    return float(t), False


def split_specs(project, sec, note_id=None, snap_sec=0.0):
    """t（編集前の秒）でノートを分ける編集。(外す id, 入れる spec, info)"""
    t, snapped = snap_time(project, float(sec), snap_sec)
    t = round(t, 4)
    ns = project.take_notes
    i = _find_split(ns, t)
    if i is None or (note_id and ns[i].id != note_id):
        where = ("%s の中" % note_id) if note_id else "区間の中"
        raise NoteEditError("%.3f s は%sではない（両端から %.0f ms 以上内側で分ける）"
                            % (t, where, MIN_PIECE_SEC * 1000))
    n = ns[i]
    rid = right_id(n.id, t)
    rm1, add1 = _retarget(project, [n.id])
    rm2, add2 = _repair_pairs(project, {n.id: rid}, set())
    spec = {"kind": "split", "target": Target.range(t, t),
            "params": {"note_id": n.id, "right_id": rid}}
    return rm1 + rm2, add1 + add2 + [spec], {
        "sec": t, "snapped": snapped, "left": n.id, "right": rid,
        "left_span": [n.start_sec, t], "right_span": [t, n.end_sec]}


def merge_specs(project, note_a, note_b):
    """接して並ぶ同じ種類の区間を 1 つにする編集。(外す id, 入れる spec, info)"""
    ns = project.take_notes
    ix = {n.id: i for i, n in enumerate(ns)}
    if note_a not in ix or note_b not in ix:
        raise NoteEditError("ノートが無い: %s / %s（list_notes で確認）" % (note_a, note_b))
    a, b = ns[ix[note_a]], ns[ix[note_b]]
    if ix[note_b] != ix[note_a] + 1 or a.kind != b.kind \
            or abs(b.start_sec - a.end_sec) > MERGE_TOL_SEC:
        raise NoteEditError("%s と %s は接して並ぶ同じ種類の区間ではない（間に隙間か別の区間がある）"
                            % (note_a, note_b))
    t = a.end_sec
    rm1, add1 = _retarget(project, [a.id, b.id])
    rm2, add2 = _repair_pairs(project, {b.id: a.id}, {(a.id, b.id)})
    rm4, add4 = _merge_timing(project, a, b, t)
    # ユーザーが分割した境目なら、その split を外すだけ（編集リストを太らせない）
    own = [e for e in project.edits if e.kind == "split"
           and abs(float(e.target.start_sec) - t) <= MERGE_TOL_SEC]
    if own:
        rm3, add3 = [own[-1].id], []
    else:
        rm3, add3 = [], [{"kind": "merge", "target": Target.range(t, t),
                          "params": {"a": a.id, "b": b.id}}]
    return rm1 + rm2 + rm3 + rm4, add1 + add2 + add3 + add4, {
        "sec": t, "note_id": a.id, "span": [a.start_sec, b.end_sec],
        "removed_split": bool(own), "removed_silence": bool(rm4)}


def _merge_timing(project, a, b, t):
    """結合する境目 t にある無音の挿入（`silence`）を外す編集。(外す id, 入れる spec)

    ノートの中に無音の穴を残さないため。外したぶんは、結合したノート全体（a の頭〜b の尻）を
    伸ばして埋める（頭と尻の編集後の位置は変えない = 後ろはずらさない）。"""
    from .timing import TIMING_KINDS, _dedup, _emit, current_map
    sil = [e for e in project.edits if e.kind == "silence"
           and abs(float(e.target.start_sec) - t) <= MERGE_TOL_SEC]
    if not sil:
        return [], []
    tm = current_map(project)
    s0, s1 = float(a.start_sec), float(b.end_sec)
    es, ee = tm.at(s0, "right"), tm.at(s1, "left")
    d = sum(float(e.params["sec"]) for e in sil)
    if ee - d - es < 0.01:
        return [], []                                  # 穴の方が長い: 触らない
    W0, W1 = s0, s1
    ids = set()
    while True:
        grow = False
        for e in project.edits:
            if e.kind not in TIMING_KINDS or e.id in ids:
                continue
            x, y = project.edit_span(e)
            if y >= W0 - 1e-9 and x <= W1 + 1e-9:
                ids.add(e.id)
                if x < W0:
                    W0, grow = float(x), True
                if y > W1:
                    W1, grow = float(y), True
        if not grow:
            break
    if any(e.kind == "move" for e in project.edits if e.id in ids):
        return [], []                                  # 旧式の move が絡む: 触らない
    pts = tm.points(W0, "left", W1, "right")
    k = (ee - es) / (ee - d - es)
    out, cut = [], 0.0
    for i, (s, o) in enumerate(pts):
        if (i > 0 and abs(s - pts[i - 1][0]) < 1e-12 and abs(s - t) <= MERGE_TOL_SEC
                and o > pts[i - 1][1] + 1e-12 and cut < d - 1e-9):
            cut += o - pts[i - 1][1]                   # 無音（同じ秒の縦の段）を詰める
            continue
        if s0 - 1e-12 <= s <= s1 + 1e-12 and not (s == s1 and o > ee + 1e-12):
            o = es + (o - cut - es) * k
        out.append((s, o))
    removes = [e.id for e in project.edits if e.id in ids]
    return removes, _emit(_dedup(out))
