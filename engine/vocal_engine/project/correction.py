# -*- coding: utf-8 -*-
"""補正の度合いと「最後にかけたのが手動か自動か」（issue #37）。

画面はノートごとに、タイミング（帯）とピッチ（線）の色を
**自動補正の度合い**（黄 → 赤へ連続）と**手動（白）**で塗り分ける。そのための値をここで作る。

## 度合い（0〜1）= 元からどれだけ動いたか ÷ 基準

| | 動いた量 | 基準（度合い 1） |
|---|---|---|
| タイミング | ノートの頭・尻・中の折れ点（時間写像の節）のうち、**編集前の秒から編集後の秒への移動**が最大のもの | `TIMING_REF_MS` = 80 ms |
| ピッチ | ノートのフレームの**ずらし量（基本の段 pitch_shift / pitch_curve）の平均**の絶対値 | `PITCH_REF_CENTS` = 100 セント（半音） |

度合い = min(1, 動いた量 ÷ 基準)。基準は「ガイドに合わせる」で動く量の目安から決めた:
タイミングは 16 分音符（120 BPM で 125 ms）の 2/3 ほどで赤に届き、発音の頭の 10〜30 ms の揃えは
黄〜橙に収まる。ピッチは半音ずらしたら赤、10〜30 セントの微調整は黄に近い橙。
「動いた量」は**今の状態から測る**（何回に分けて当てたかによらない）ので、画面はドラッグ中・
「ガイドに合わせる」のスライダー中も同じ式で色を決められる（ドラッグ中の見た目＝離した後）。

補正あり = タイミングは動いた量が 0.1 ms を超える、ピッチはずらし量の最大が 0.1 セントを超えるか
鉛筆（`pitch_draw`）が重なる。

## 手動か自動か = 最後にそのノートを動かした changeset の由来

編集を当てるとき（`Project.apply_edits` / `apply_changes`）に、**当てる前と後でタイミング・ピッチが
変わったノート**を数えて、changeset に印（`{"op": "mark", "origin": "auto" | "manual", "timing": [...], "pitch": [...]}`）
を入れる。取り消し（undone）の changeset の印は数えないので、取り消し・やり直しでも正しく戻る。
自動 = 「ガイドに合わせる」（`plan_guide` の計画・`correct_to_guide`）。それ以外（ドラッグ・鉛筆・半音に合わせる・
MCP の shift_pitch など）は手動。接続された隣が一緒に動いたときは、その隣も同じ由来になる。
印の無いノート（この機能より前に作った編集）は自動として度合いで塗る。
分割でできた右の片は、分割前のノートの印を引き継ぐ。
"""
import numpy as np

TIMING_REF_MS = 80.0
PITCH_REF_CENTS = 100.0
EPS_SEC = 1e-4
EPS_CENTS = 0.1
TIMING_KINDS = ("move", "stretch", "silence", "crop", "move_boundary")
PITCH_KINDS = ("pitch_shift", "pitch_curve", "pitch_draw")
ORIGINS = ("auto", "manual")


# ---------------------------------------------------------------- 状態
def _state(project):
    """当てる前後で比べる状態（時間写像・ずらし量・鉛筆の範囲）。解析前なら None。"""
    if getattr(project, "_take_f0", None) is None:
        return None
    from ..render.pipeline import edits_to_segments
    from ..view.export_data import build_time_map, cents_offset
    segs = edits_to_segments(project.edits, project.edit_span)
    src, out = build_time_map(segs, 0.0, max(project.duration_sec, 1e-6))
    times = project.take_f0.times
    off = cents_offset(segs, times)
    draws = sorted((float(e.target.start_sec), float(e.target.end_sec), e.id)
                   for e in project.edits if e.kind == "pitch_draw")
    return {"src": src, "out": out, "off": off, "draws": draws, "hop": project.take_f0.hop_s}


def _at(src, out, t, side):
    # 写像の外（ノートの尻が素材の長さをわずかに越える）は傾き 1 で延ばす（画面の toEdited と同じ）
    if t > src[-1]:
        return float(out[-1] + (t - src[-1]))
    if t < src[0]:
        return float(out[0] - (src[0] - t))
    v = float(np.interp(t, src, out))
    if side == "left":
        i = int(np.searchsorted(src, t, side="left"))
        if i < len(src) and src[i] == t:
            v = float(out[i])
    return v


def _interior(src, out, a, b):
    """[a, b] の中の時間写像の節 [(編集前, 編集後)]（無音の挿入の 2 値は両方）。"""
    i = int(np.searchsorted(src, a, side="right"))
    j = int(np.searchsorted(src, b, side="left"))
    return [(float(src[k]), float(out[k])) for k in range(i, j)]


def timing_shift(src, out, a, b):
    """ノート [a, b] の、編集前の秒から編集後の秒への移動の最大（秒）。"""
    m = max(abs(_at(src, out, a, "right") - a), abs(_at(src, out, b, "left") - b))
    for s, o in _interior(src, out, a, b):
        m = max(m, abs(o - s))
    return m


def _frames(n, hop, size):
    a = max(0, int(round(n.start_sec / hop)))
    b = min(size, int(round(n.end_sec / hop)))
    return a, b


def _draws_on(draws, a, b):
    return tuple(i for s, e, i in draws if e > a and s < b)


def changed_notes(project, before, after):
    """当てる前後でタイミング・ピッチが変わったノートの id。(timing, pitch)。"""
    if before is None or after is None:
        return [], []
    tim, pit = [], []
    s0, o0, s1, o1 = before["src"], before["out"], after["src"], after["out"]
    same_map = len(s0) == len(s1) and np.array_equal(s0, s1) and np.array_equal(o0, o1)
    same_off = np.array_equal(before["off"], after["off"])
    same_draws = before["draws"] == after["draws"]
    hop = after["hop"]
    for n in project.take_notes:
        a, b = float(n.start_sec), float(n.end_sec)
        if not same_map:
            pts = {a, b}
            pts.update(s for s, _ in _interior(s0, o0, a, b))
            pts.update(s for s, _ in _interior(s1, o1, a, b))
            moved = False
            for t in pts:
                for side in (("right",) if t == a else ("left",) if t == b else ("left", "right")):
                    if abs(_at(s0, o0, t, side) - _at(s1, o1, t, side)) > EPS_SEC:
                        moved = True
                        break
                if moved:
                    break
            if moved:
                tim.append(n.id)
        if not (same_off and same_draws) and n.kind == "note":
            fa, fb = _frames(n, hop, len(after["off"]))
            d = after["off"][fa:fb] - before["off"][fa:fb] if fb > fa else np.zeros(0)
            if (len(d) and float(np.max(np.abs(d))) > EPS_CENTS) or \
                    _draws_on(before["draws"], a, b) != _draws_on(after["draws"], a, b):
                pit.append(n.id)
    return tim, pit


class Marker:
    """`Project.apply_*` の前後で使う: `m = Marker(project, specs, removes)` → 当てる → `m.op()`。"""

    def __init__(self, project, kinds, origin="manual"):
        self.project = project
        self.origin = origin if origin in ORIGINS else "manual"
        self.on = any(k in TIMING_KINDS or k in PITCH_KINDS for k in kinds)
        try:
            self.before = _state(project) if self.on else None
        except Exception:                    # noqa: BLE001  印が作れなくても編集は当てる
            self.before = None

    def op(self):
        """当てた後に呼ぶ。changeset に足す印（変わったノートが無ければ None）。"""
        if self.before is None:
            return None
        try:
            tim, pit = changed_notes(self.project, self.before, _state(self.project))
        except Exception:                    # noqa: BLE001
            return None
        if not tim and not pit:
            return None
        return {"op": "mark", "origin": self.origin, "timing": tim, "pitch": pit}


def kinds_of(project, specs, remove_ids=()):
    out = [s.get("kind") for s in specs]
    rm = set(remove_ids)
    out += [e.kind for e in project.edits if e.id in rm]
    return out


# ---------------------------------------------------------------- 読む
def origins(project):
    """{note id: "auto" | "manual"} をタイミング・ピッチそれぞれ。取り消した changeset の印は数えない。"""
    tim, pit = {}, {}
    for cs in project.changesets:
        if cs.undone:
            continue
        for op in cs.ops:
            if op.get("op") != "mark":
                continue
            o = op.get("origin", "manual")
            for i in op.get("timing") or []:
                tim[i] = o
            for i in op.get("pitch") or []:
                pit[i] = o
    # 分割でできた右の片は、分割前のノートの印を引き継ぐ（その後に印が付いていなければ）
    for e in project.edits:
        if e.kind == "split":
            left, right = e.params.get("note_id"), e.params.get("right_id")
            for m in (tim, pit):
                if right and right not in m and left in m:
                    m[right] = m[left]
    return tim, pit


def degree(amount, ref):
    return float(min(1.0, max(0.0, amount / ref))) if ref > 0 else 0.0


def note_corrections(project, notes, src, out, off_base, off_drawn, hop):
    """export_view_data 用: {note id: {"timing": {...} | None, "pitch": {...} | None}}。

    timing = {"amount_ms", "degree", "manual"}、pitch = {"amount_cents", "degree", "manual", "shape"}
    （shape = 一定量のずらしでない = 鉛筆・曲線。ドラッグで平均を 0 に戻しても補正が残る）。"""
    tim_o, pit_o = origins(project)
    draws = [(float(e.target.start_sec), float(e.target.end_sec)) for e in project.edits
             if e.kind == "pitch_draw"]
    res = {}
    for n in notes:
        a, b = float(n.start_sec), float(n.end_sec)
        t = None
        sh = timing_shift(src, out, a, b)
        if sh > EPS_SEC:
            t = {"amount_ms": round(sh * 1000.0, 2),
                 "degree": round(degree(sh * 1000.0, TIMING_REF_MS), 4),
                 "manual": tim_o.get(n.id) == "manual"}
        p = None
        if n.kind == "note":
            fa, fb = _frames(n, hop, len(off_base))
            has_draw = any(e > a and s < b for s, e in draws)
            # 鉛筆は off_base に入らない。実際に表示する曲線の変位を測る。
            v = (off_drawn if has_draw else off_base)[fa:fb] if fb > fa else np.zeros(0)
            amount = float(np.max(np.abs(v))) if len(v) else 0.0
            if amount > EPS_CENTS or has_draw:
                shape = has_draw or (len(v) > 0 and float(np.max(v) - np.min(v)) > EPS_CENTS)
                p = {"amount_cents": round(amount, 2),
                     "degree": round(degree(amount, PITCH_REF_CENTS), 4),
                     "manual": pit_o.get(n.id) == "manual", "shape": bool(shape)}
        res[n.id] = {"timing": t, "pitch": p}
    return res
