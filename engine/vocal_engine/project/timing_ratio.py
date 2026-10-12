# -*- coding: utf-8 -*-
"""「ガイドに合わせる」のタイミングの割合を**ノートごとに**変える（2026-10-11 承認・案 T1）。

ガイドの changeset の記録（`project/strength.py`）に、ノートごとのタイミングの割合 `timing` と
100% の動き（節 `timing_nodes` の `d`）が残っている。ここでは、選んだノートの節を

    位置 = 0% の位置 + 割合 × d          （0% の位置 = 今の位置 − 今の割合 × d − y）

へ動かす。y は、そのノートの節が割合のとおりの位置から外れている量（隣にゆずった・届かなかった・
「オリジナルに戻す」で境目を戻された）。記録の節に `"y"` として残す（0 なら書かない）。
割合をそろえて動かす（全部のノートを s に）と、記録の無い曲では `apply_plan(x=s)` と同じ位置になる。

## 動かすもの・動かさないもの

- **選んだノート**: 割合の記録が生きている（timing_state = ok）ノートは割合を変える（change）。記録の無い
  ノート（none）と、restart に入れた変えられないノート（stale・unsupported）は、今の状態を 0% として計画の 100% の動き（`plan.recipe_timing`）で
  合わせる（fresh）。合わせた後に手で直したノート（timing_manual）は、include_manual のときだけ動かし、
  そのときは手で直したずれを残す（割合の差 × d だけ動かす）。変えられないノート（stale・前の形の記録・
  節が見つからない）は動かさず、理由を返す。
- 1 つのスライダーで、ノートの頭・そのノートの切り離した尻・休みの手前の音節の終わりが一緒に動く。
  節の持ち主は記録のまま（頭の組 → そのノート、接続した境目 → 次のノート、切り離した尻 → そのノート）。
- **ゆずる**: 動かすノートの頭が、隙間を挟んだ前のノートの切り離した尻を追い越すときは、前のノートの尻が
  ゆずって短くなる（前のノートの記録が生きていて、手で直しておらず、動かさないときだけ）。ゆずれないとき
  （手で直した・記録が無い・短すぎる）は、選んだノートが手前で止まる（届かない）。制約は破らない。
- 選んでいないノートの節は動かさない（ノートの中の発音の頭・音節の終わりも、その位置に留める）。

## 割合が混ざるとき

割合 s での目標は s について線形（I(s) = I0 + s·(I1 − I0)）。0% と 100% でそれぞれ制約を直し（`_repair`）、
節の動きを D(s) = D0 + s·(D1 − D0) とする。制約は節の動きについて凸なので、間の s も制約を守る
（確定のときにもう一度確かめ、破っていればその s で直す）。画面のプレビューは節ごとの
`[素材の秒, 今の秒, base, move]` から「今の秒 + base + move·s」で描く（planWarp と同じ形）。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import numpy as np

from . import strength as SG
from . import timing as TM

MATCH_TOL = 2e-6          # 記録の節と骨組みの節を同じとみなす素材の秒のずれ（記録は 6 桁に丸めている）
REACH_TOL = 5e-4          # 目標からこれ以上離れたら「届かない」
MOVE_TOL = 1e-4           # 隣のノートの端がこれ以上動いたら「長さも変わる」
Y_TOL = 1e-6              # これより小さい y は書かない
HOLD_REASON = "前のノートをオリジナルに戻した境目は動かさない"
NODES_REASON = "ノートの区切り・つながりが変わった（記録の節が見つからない）"
MSG_NEIGHBOURS = "接している前のノートの長さも変わります（{n} ノート）"
MSG_UNREACHABLE = "{n} ノートは届きません"
PRIO_YIELD, PRIO_SOFT, PRIO_HARD = 0, 1, 2


class RatioError(TM.TimingError):
    pass


@dataclass
class RatioPlan:
    st: object
    sel: list
    notes: dict               # 動かすノート → {"kind": change|fresh, "s": 今の割合, "manual", "nodes": [(節, k)]}
    recorded: dict            # 記録が生きているノート → {"s", "nodes": [(節, k)]}（動かさないノートも）
    keys: dict                # key → [knot]（組なら組の全部）
    I0: dict                  # 動かす key の目標（0% / 100%）
    I1: dict
    prio: dict                # 動かせる key → 優先度
    fixed: dict               # 動かさない key → 値（0）
    val0: dict
    val1: dict
    blocked: dict             # key → (理由, 妨げたノート)
    held: set                 # 「オリジナルに戻す」で境目を戻されたので動かさない key
    yields: dict              # ゆずる key → 前のノート
    D0: np.ndarray
    D1: np.ndarray
    excluded: list = field(default_factory=list)
    unchangeable: list = field(default_factory=list)
    manual: set = field(default_factory=set)   # 合わせた後にタイミングを手で直したノート（全部）
    sig: dict | None = None
    hop: float = 0.01
    state: tuple = ()


# ================================================================ 入口
def ratio_plan(project, plan, include_manual=False, restart=()):
    """計画（`plan_guide`）から、タイミングの割合を変える骨組みと 0%・100% の動きを作る（同じ状態なら使い回す）。
    記録に残すタイミングの動きの無い計画は None。"""
    rec = plan.recipe_timing
    if rec is None:
        return None
    key = (TM.state_sig(project), bool(include_manual), tuple(sorted(restart or ())))
    hit = plan.ratio_cache.get(key)
    if hit is not None:
        return hit
    rp = _build(project, plan, rec, bool(include_manual), set(restart or ()))
    plan.ratio_cache.clear()
    plan.ratio_cache[key] = rp
    return rp


def preview(project, plan, include_manual=False, restart=()):
    """画面のプレビュー（計画の JSON の `timing_ratio`）。"""
    rp = ratio_plan(project, plan, include_manual, restart)
    return None if rp is None else to_json(rp)


# ================================================================ 骨組みと節
def _owners(st):
    """節 → 持ち主のノート（`timing._timing_nodes` と同じ規則。組の無い節はそのノート、固定の端は None）。"""
    own = [kn.note for kn in st.knots]
    for u, ks in st.units.items():
        if u in st.fixed_units or not ks:
            for k in ks:
                own[k] = None
            continue
        heads = [k for k in ks if st.knots[k].role in ("note_start", "attack")]
        o = st.knots[heads[0]].note if heads else st.knots[ks[0]].note
        for k in ks:
            own[k] = o
    return own


def _insert_many(st, tm, items):
    """節を足す（`timing._insert_knot` を一度にまとめたもの。番号の付け直しは 1 回）。
    items = [(素材の秒, ノート)]。足せない（そのノートの piece の中に無い）ものを返す。"""
    by_note = {}
    for p, pc in enumerate(st.pieces):
        if pc.note is not None:
            by_note.setdefault(pc.note, []).append(p)
    adds, bad = {}, []
    for t, nid in items:
        ps = by_note.get(nid) or []
        p = next((p for p in ps if st.knots[st.pieces[p].a].src <= t <= st.knots[st.pieces[p].b].src), None)
        if p is None:
            bad.append((t, nid))
            continue
        lst = adds.setdefault(p, [])
        if not any(abs(t - u) < 1e-6 for u, _ in lst):
            lst.append((t, nid))
    if not adds:
        return bad
    knots, pieces, old2new, pmap = [], [], {}, {}
    for k, kn in enumerate(st.knots):
        old2new[k] = len(knots)
        knots.append(kn)
        if k >= len(st.pieces):
            continue
        pc = st.pieces[k]
        pmap[k] = len(pieces)
        for t, nid in sorted(adds.get(k, [])):
            nk = TM.Knot(src=float(t), side="right", role="inner", note=nid)
            nk.cur = tm.at(nk.src, "right")
            pieces.append(TM.Piece(a=len(knots) - 1, b=len(knots), mode=pc.mode, note=pc.note,
                                   rmin=pc.rmin, rmax=pc.rmax))
            knots.append(nk)
        pieces.append(TM.Piece(a=len(knots) - 1, b=len(knots), mode=pc.mode, note=pc.note,
                               rmin=pc.rmin, rmax=pc.rmax))
    st.knots, st.pieces = knots, pieces
    st.units = {u: [old2new[k] for k in ks] for u, ks in st.units.items()}
    st.note_knots = {n: (old2new[a], old2new[b]) for n, (a, b) in st.note_knots.items()}
    st.gap_of_pair = {k: pmap[v] for k, v in st.gap_of_pair.items()}
    return bad


def _match(st, own, owned_units, nodes, nid):
    """記録の節 → 骨組みの節の番号（全部見つかれば [(節, k)]、どれか見つからなければ None）。"""
    srcs = [kn.src for kn in st.knots]
    out = []
    for nd in nodes:
        t = float(nd["src"])
        side = "left" if nd.get("side") == "l" else "right"
        i = bisect.bisect_left(srcs, t - MATCH_TOL)
        k = None
        while i < len(srcs) and srcs[i] <= t + MATCH_TOL:
            if st.knots[i].side == side and own[i] == nid:
                k = i
                break
            i += 1
        if k is None:
            return None
        out.append((nd, k))
    # 持ち主の組（固定でない）はどれも記録の節がある（無ければ、つながり・アタックが変わった）
    units = {st.knots[k].unit for _, k in out if st.knots[k].unit is not None}
    if units != owned_units.get(nid, set()):
        return None
    hk = st.note_knots.get(nid, (None,))[0]
    if hk is None or st.knots[hk].unit not in units:
        return None
    return out


def _attacks(project, nodesets):
    """記録の節から、ノートの頭の手前に広げたアタックの頭 {ノート: 秒}（`build_structure` の attacks）。"""
    start = {n.id: n.start_sec for n in TM.pitched_notes(project)}
    out = {}
    for nid, nodes in nodesets.items():
        s = start.get(nid)
        if s is None:
            continue
        ts = [float(n["src"]) for n in nodes if n.get("side") == "r" and float(n["src"]) < s - 1e-6]
        if ts:
            out[nid] = min(ts)
    return out


def _order(project):
    """({ノート: 最後のタイミングの記録の changeset の順}, {ノート: 最後に外した順})。取り消したものは数えない。"""
    rec, void = {}, {}
    for i, cs in enumerate(project.changesets):
        if cs.undone:
            continue
        for op in cs.ops:
            k = op.get("op")
            if k == "recipe" and op.get("kind", "guide") == "guide":
                for nid, ent in (op.get("notes") or {}).items():
                    if "timing" in ent:
                        rec[nid] = i
            elif k == "recipe_void" and "timing" in (op.get("parts") or ("pitch", "timing")):
                for nid in op.get("notes") or []:
                    void[nid] = i
    return rec, void


# ================================================================ 組み立て
def _build(project, plan, rec, include_manual, restart):
    tn = {n.id: n for n in TM.pitched_notes(project)}
    want = plan.params.get("note_ids")
    sel = sorted((set(want) if want else set(tn)) & set(tn))
    rows = SG.states(project, list(tn), sig=rec.get("sig"))
    _, tlatest = SG.recipes(project, "timing")
    plan_nodes = rec.get("nodes") or {}
    rp_unchangeable, rp_excluded = [], []
    recorded_nodes = {}
    for nid in tn:
        r = rows.get(nid) or {}
        if r.get("timing_state") == "ok":
            op = tlatest[nid][0]
            recorded_nodes[nid] = [dict(n) for n in op.get("timing_nodes") or [] if n.get("note") == nid]
    moving = {}
    for nid in sel:
        r = rows[nid]
        ts = r["timing_state"]
        if ts == "none" or (nid in restart and ts in ("stale", "unsupported")):
            if not plan_nodes.get(nid):
                rp_unchangeable.append({"note": nid, "reason": SG.REASONS["material"]})
                continue
            kind = "fresh"
        elif ts == "ok":
            kind = "change"
        else:
            rp_unchangeable.append({"note": nid, "reason": r["timing_reason"]})
            continue
        if r["timing_manual"] and not include_manual:
            rp_excluded.append(nid)
            continue
        moving[nid] = {"kind": kind, "manual": bool(r["timing_manual"]),
                       "s": float(r["timing"] or 0.0) if kind == "change" else 0.0}
    nodesets = {nid: recorded_nodes[nid] for nid in recorded_nodes if nid not in moving}
    for nid, m in moving.items():
        nodesets[nid] = ([dict(n) for n in plan_nodes[nid]] if m["kind"] == "fresh"
                         else recorded_nodes[nid])
    st, tm = TM.build_structure(project, guide=True, confirmed_only=True,
                                attacks=_attacks(project, nodesets))
    # 足りない節（ノートの中の発音の頭・音節の終わり）を足す
    have = {}
    for k, kn in enumerate(st.knots):
        have.setdefault(round(kn.src, 6), set()).add(kn.side)
    need = []
    for nid, nodes in nodesets.items():
        for nd in nodes:
            t = float(nd["src"])
            if nd.get("side") == "r" and not any(
                    "right" in have.get(round(t + e, 6), ()) for e in (-1e-6, 0.0, 1e-6)):
                need.append((t, nid))
    _insert_many(st, tm, need)
    own = _owners(st)
    owned_units = {}
    for k, kn in enumerate(st.knots):
        if own[k] is not None and kn.unit is not None and kn.unit not in st.fixed_units:
            owned_units.setdefault(own[k], set()).add(kn.unit)
    matched = {}
    for nid, nodes in nodesets.items():
        mk = _match(st, own, owned_units, nodes, nid)
        if mk is None:
            if nid in moving:
                del moving[nid]
                rp_unchangeable.append({"note": nid, "reason": NODES_REASON})
            continue
        matched[nid] = mk
    for nid, m in moving.items():
        m["nodes"] = matched[nid]
    recorded = {nid: {"s": float(rows[nid]["timing"] or 0.0), "nodes": matched[nid],
                      "manual": bool(rows[nid]["timing_manual"])}
                for nid in recorded_nodes if nid in matched}

    def key_of(k):
        u = st.knots[k].unit
        return ("u", u) if u is not None else ("k", k)

    keys = {}
    for nid in matched:
        for _, k in matched[nid]:
            kk = key_of(k)
            keys[kk] = list(st.units[kk[1]]) if kk[0] == "u" else [k]
    # ---- 動かす key の目標（0% と 100%）
    acc0, acc1, prio, held = {}, {}, {}, set()
    rec_i, void_i = _order(project)
    for nid, m in moving.items():
        hk = st.note_knots[nid][0]
        hu = st.knots[hk].unit
        hold_units = set()
        if m["kind"] == "change" and hu is not None:      # 前のノートを後から「オリジナルに戻す」した境目
            for k in st.units[hu]:
                v = st.knots[k].note
                if v != nid and void_i.get(v, -1) > rec_i.get(nid, -1):
                    hold_units.add(hu)
        for nd, k in m["nodes"]:
            kk = key_of(k)
            d = float(nd.get("d") or 0.0)
            y = 0.0 if m["kind"] == "fresh" else float(nd.get("y") or 0.0)
            if m["manual"]:
                y = 0.0                                    # 手で直したずれを残す
            i0, i1 = -m["s"] * d - y, (1.0 - m["s"]) * d - y
            acc0.setdefault(kk, []).append(i0)
            acc1.setdefault(kk, []).append(i1)
            p = PRIO_HARD if nd.get("role") == "anchor" else PRIO_SOFT
            prio[kk] = max(prio.get(kk, 0), p)
            if (m["kind"] == "change" and nd.get("hold")) or (kk[0] == "u" and kk[1] in hold_units):
                held.add(kk)
    I0 = {k: float(np.mean(v)) for k, v in acc0.items()}
    I1 = {k: float(np.mean(v)) for k, v in acc1.items()}
    fixed = {}
    for kk in held:
        prio.pop(kk, None)
        fixed[kk] = 0.0
    # 動かさないノートの、組の無い節（ノートの中の発音の頭・音節の終わり）はその位置に留める
    for nid, mk in matched.items():
        if nid in moving:
            continue
        for _, k in mk:
            kk = key_of(k)
            if kk[0] == "k" and kk not in I0:
                fixed[kk] = 0.0
    # ---- ゆずる: 動かすノートの頭の前の隙間の、前のノートの切り離した尻
    yields = {}
    for nid in moving:
        hk = st.note_knots[nid][0]
        hu = st.knots[hk].unit
        k0 = min(st.units[hu]) if hu is not None else hk
        if k0 < 1 or st.pieces[k0 - 1].mode != "gap":
            continue
        kv = k0 - 1
        v = st.knots[kv].note
        vu = st.knots[kv].unit
        if (v is None or v in moving or v not in recorded or recorded[v]["manual"]
                or st.knots[kv].role != "note_end" or vu is None):
            continue
        kk = ("u", vu)
        if kk not in keys or kk in fixed or kk in I0:
            continue
        ys = [float(nd.get("y") or 0.0) for nd, k in recorded[v]["nodes"] if key_of(k) == kk]
        if any(nd.get("hold") for nd, k in recorded[v]["nodes"] if key_of(k) == kk):
            continue
        tgt = -float(np.mean(ys)) if ys else 0.0
        I0[kk] = I1[kk] = tgt
        prio[kk] = PRIO_YIELD
        yields[kk] = v
    val0, blocked0 = _repair(st, key_of, keys, {k: I0[k] for k in prio}, prio, fixed)
    val1, blocked1 = _repair(st, key_of, keys, {k: I1[k] for k in prio}, prio, fixed)
    blocked = dict(blocked0)
    blocked.update(blocked1)
    D0 = TM.solve(st, _anchors(keys, val0, fixed)).d
    D1 = TM.solve(st, _anchors(keys, val1, fixed)).d
    return RatioPlan(st=st, sel=sel, notes=moving, recorded=recorded, keys=keys, I0=I0, I1=I1,
                     prio=prio, fixed=fixed, val0=val0, val1=val1, blocked=blocked, held=held,
                     yields=yields, D0=D0, D1=D1, excluded=sorted(rp_excluded),
                     unchangeable=sorted(rp_unchangeable, key=lambda r: r["note"]),
                     manual={n for n, r in rows.items() if r.get("timing_manual")},
                     sig=rec.get("sig"), hop=float(rec.get("hop") or 0.01),
                     state=TM.state_sig(project))


def _anchors(keys, val, fixed):
    out = {}
    for kk, v in list(val.items()) + list(fixed.items()):
        out[keys[kk][0] if kk in keys else kk[1]] = float(v)
    return out


def _repair(st, key_of, keys, val, prio, fixed, max_iter=400):
    """目標 val（key → 動き）で破る制約を直す。固定の key は動かさない。両側とも動かせるなら優先度の
    低い側を、同じなら半分ずつ動かす。返り値: (直した val, {動かした key: (理由, 妨げたノート)})。"""
    val = dict(val)
    blocked = {}

    def owner_note(k):
        return st.knots[k].note

    for _ in range(max_iter):
        sol = TM.solve(st, _anchors(keys, val, fixed))
        bad = sorted((c for c in sol.constraints if c[0] + c[1] < -1e-9), key=lambda c: c[0] + c[1])
        if not bad:
            return val, blocked
        touched = set()
        progress = False
        for a, b, why, ka, kb, sgn in bad:
            need = -(a + b) / sgn                     # d[kb] - d[ka] をこれだけ変える
            fa, fb = key_of(ka), key_of(kb)
            if fa in touched or fb in touched:
                continue
            ma, mb = fa in prio and fa != fb, fb in prio and fa != fb
            if ma and mb:
                if prio[fa] < prio[fb]:
                    val[fa] -= need
                    moved = [(fa, kb)]
                elif prio[fb] < prio[fa]:
                    val[fb] += need
                    moved = [(fb, ka)]
                else:
                    val[fa] -= need / 2.0
                    val[fb] += need / 2.0
                    moved = [(fa, kb), (fb, ka)]
            elif mb:
                val[fb] += need
                moved = [(fb, ka)]
            elif ma:
                val[fa] -= need
                moved = [(fa, kb)]
            else:
                continue
            for kk, other in moved:
                blocked.setdefault(kk, (why, owner_note(other)))
            touched.update((fa, fb))
            progress = True
        if not progress:
            break
    # 直しきれない: 動かす量を 0（今の位置。制約を守る）に向けて縮める
    base = dict(val)
    lo, hi = 0.0, 1.0
    for _ in range(30):
        mid = 0.5 * (lo + hi)
        trial = {k: v * mid for k, v in base.items()}
        sol = TM.solve(st, _anchors(keys, trial, fixed))
        if any(c[0] + c[1] < -1e-9 for c in sol.constraints):
            hi = mid
        else:
            lo = mid
    val = {k: v * lo for k, v in base.items()}
    for kk in val:
        blocked.setdefault(kk, ("制約を直しきれない", None))
    return val, blocked


# ================================================================ プレビュー
def _miss(rp, kk):
    """key の目標からのずれ (0% で, 100% で)。"""
    if kk in rp.held:
        return 0.0 - rp.I0[kk], 0.0 - rp.I1[kk]
    return rp.val0[kk] - rp.I0[kk], rp.val1[kk] - rp.I1[kk]


def _note_keys(rp, nid):
    st = rp.st
    out = []
    for _, k in rp.notes[nid]["nodes"]:
        u = st.knots[k].unit
        kk = ("u", u) if u is not None else ("k", k)
        if kk not in out:
            out.append(kk)
    return out


def unreachable(rp, s=None):
    """届かないノート [{note, reason, blocker, miss: [0% での, 100% での]}]。s を渡すと、その割合で届くものは外す。"""
    out = []
    for nid in sorted(rp.notes):
        worst, why = None, None
        for kk in _note_keys(rp, nid):
            if kk not in rp.I0:
                continue
            g0, g1 = _miss(rp, kk)
            at = max(abs(g0), abs(g1)) if s is None else abs(g0 + float(s) * (g1 - g0))
            if at > REACH_TOL and (worst is None or at > worst[0]):
                worst = (at, g0, g1)
                why = (HOLD_REASON, None) if kk in rp.held else rp.blocked.get(kk, ("届かない", None))
        if worst is not None:
            out.append({"note": nid, "reason": why[0], "blocker": why[1],
                        "miss": [round(worst[1], 6), round(worst[2], 6)]})
    return out


def neighbours(rp):
    """動かさないのに端が動くノート [{note, head, tail}]（端 = [今の秒, base, move] か None）。"""
    st = rp.st
    out = []
    for nid, (ks, ke) in st.note_knots.items():
        if nid in rp.notes:
            continue
        ends = []
        for k in (ks, ke):
            b, m = float(rp.D0[k]), float(rp.D1[k] - rp.D0[k])
            moved = max(abs(b), abs(b + m)) > MOVE_TOL
            ends.append([TM._r(st.knots[k].cur), TM._r(b), TM._r(m)] if moved else None)
        if ends[0] is not None or ends[1] is not None:
            out.append({"note": nid, "head": ends[0], "tail": ends[1]})
    out.sort(key=lambda r: r["note"])
    return out


def to_json(rp):
    st = rp.st
    ks = st.knots
    mv = [i for i in range(len(ks)) if abs(rp.D0[i]) > 1e-12 or abs(rp.D1[i]) > 1e-12]
    a, b = (max(0, mv[0] - 1), min(len(ks) - 1, mv[-1] + 1)) if mv else (0, -1)
    nb = neighbours(rp)
    ur = unreachable(rp)
    return {
        "changed": [{"note": n, "timing": round(m["s"], 6), "manual": m["manual"]}
                    for n, m in sorted(rp.notes.items()) if m["kind"] == "change"],
        "fresh": [{"note": n, "manual": m["manual"]}
                  for n, m in sorted(rp.notes.items()) if m["kind"] == "fresh"],
        "excluded_manual": list(rp.excluded),
        "unchangeable": list(rp.unchangeable),
        "neighbours": nb,
        "unreachable": ur,
        "messages": {"neighbours": MSG_NEIGHBOURS.format(n=len(nb)) if nb else None,
                     "unreachable": MSG_UNREACHABLE.format(n=len(ur)) if ur else None},
        "message_templates": {"neighbours": MSG_NEIGHBOURS, "unreachable": MSG_UNREACHABLE},
        # 節: [素材の秒, 今の秒, base, move, 側]。割合 s での編集後の秒 = 今の秒 + base + move·s
        "knots": [[TM._r(ks[i].src), TM._r(ks[i].cur), TM._r(rp.D0[i]), TM._r(rp.D1[i] - rp.D0[i]),
                   "l" if ks[i].side == "left" else "r"] for i in range(a, b + 1)],
        "pieces": [st.pieces[p].mode[0] for p in range(len(st.pieces))
                   if st.pieces[p].a >= a and st.pieces[p].b <= b],
    }


# ================================================================ 確定
def realize(project, rp, s):
    """割合 s で確定したときの (外す編集, 入れる spec, info, 記録に書くもの, 印から外すノート)。

    記録に書くもの = {ノート: {"timing": 割合, "nodes": [節]}}（`strength.with_timing_ratio`）。"""
    if rp.state != TM.state_sig(project):
        raise RatioError("計画を作った後に曲が変わった（plan_edit をやり直す）")
    s = float(np.clip(float(s), 0.0, 1.0))
    st = rp.st
    val = {k: rp.val0[k] + s * (rp.val1[k] - rp.val0[k]) for k in rp.val0}
    sol = TM.solve(st, _anchors(rp.keys, val, rp.fixed))
    if any(c[0] + c[1] < -1e-9 for c in sol.constraints):
        tgt = {k: rp.I0[k] + s * (rp.I1[k] - rp.I0[k]) for k in rp.prio}

        def key_of(k):
            u = st.knots[k].unit
            return ("u", u) if u is not None else ("k", k)
        val, _ = _repair(st, key_of, rp.keys, tgt, rp.prio, rp.fixed)
        sol = TM.solve(st, _anchors(rp.keys, val, rp.fixed))
    d = sol.d
    tmp = TM.Plan(id="ratio", kind="guide", params={}, st=st, d=d, x_lo=0.0, x_hi=1.0)
    removes, specs, info = TM.realize(project, tmp, 1.0)
    info = dict(info, x=None, timing=round(s, 6))
    writes = {}

    def node_out(nd, y, hold=False):
        o = {k: nd[k] for k in ("note", "src", "side", "role", "d")}
        if abs(y) >= Y_TOL:
            o["y"] = round(y, 6)
        if hold or nd.get("hold"):
            o["hold"] = 1
        return o

    for nid, m in rp.notes.items():
        changed = m["kind"] == "fresh" or abs(s - m["s"]) > 1e-9
        out = []
        for nd, k in m["nodes"]:
            u = st.knots[k].unit
            kk = ("u", u) if u is not None else ("k", k)
            y_old = 0.0 if m["kind"] == "fresh" else float(nd.get("y") or 0.0)
            y = y_old + float(d[k]) - (s - m["s"]) * float(nd.get("d") or 0.0)
            hold = kk in rp.held
            if abs(y - y_old) >= Y_TOL or (hold and not nd.get("hold")):
                changed = True
            out.append(node_out(nd, y, hold))
        if changed:
            writes[nid] = {"timing": round(s, 6), "nodes": out}
    for kk, v in rp.yields.items():
        k0 = rp.keys[kk][0]
        if abs(float(d[k0])) < Y_TOL:
            continue
        r = rp.recorded[v]
        out = []
        for nd, k in r["nodes"]:
            u = st.knots[k].unit
            y = float(nd.get("y") or 0.0) + (float(d[k]) if ("u", u) == kk else 0.0)
            out.append(node_out(nd, y))
        writes[v] = {"timing": round(r["s"], 6), "nodes": out}
    moved_notes = set(rp.notes)
    unmarked = sorted(rp.manual - moved_notes)
    ur = unreachable(rp, s)
    nb = neighbours(rp)
    info["timing_ratio"] = {
        "notes": sorted(n for n in writes if n in rp.notes),
        "yielded": sorted(rp.yields[kk] for kk in rp.yields
                          if abs(float(d[rp.keys[kk][0]])) >= Y_TOL),
        "neighbours": [r["note"] for r in nb
                       if any(e is not None and abs(e[1] + e[2] * s) > MOVE_TOL
                              for e in (r["head"], r["tail"]))],
        "unreachable": ur, "excluded_manual": list(rp.excluded),
        "unchangeable": list(rp.unchangeable)}
    return removes, specs, info, writes, unmarked
