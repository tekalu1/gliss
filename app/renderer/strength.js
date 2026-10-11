// 「ガイドに合わせる」の割合（音程・線の形）から線を作る式。エンジンの `project/strength.py` を写したもの。
//
//   計画（`plan_edit(op="guide")`）の `strength` に、ノートごとの今の割合と材料（区切り）が載る。
//   ポップアップのつまみを動かす間は、確定（`strength.realize`）と同じ式でフレームの MIDI を作って描く。
//   式: フレームの ε（100% の目標 − 補正前。半音）、E = 2^(ε/12) − 1、ノートの C = ε の平均、K = 2^(C/12) − 1、
//   D = as·E + (ac − as)·K、MIDI = m0 + 12·log2(1 + w·D)（w = 対応区間の端の 40 ms のなだらかさ）。
//   割合の違う隣とは両側 20 ms で D を混ぜ、変えられない隣へは今の値へ 40 ms でつなぐ。
//   DOM を触らない（単体の試験 tests/unit/strength.spec.js がエンジンの値と突き合わせる）。

export const EDGE = 0.04;
export const HALF = 0.02;
export const LOCK = 0.04;
const SCALE = 1000;
const F_MIN = 0.05;

const smooth = (u) => { const v = Math.min(1, Math.max(0, u)); return v * v * (3 - 2 * v); };
const r6 = (x) => Math.round(x * 1e6) / 1e6;

class Seg {
  constructor(d, hop) {
    this.n = d.n; this.p = d.p ?? null; this.x = d.x ?? null;
    let acc = 0;
    this.m0 = d.m0.map((v) => { acc += v; return acc / SCALE; });
    this.e = d.e.map((v) => v / SCALE);
    this.t = this.m0.map((_, k) => (d.i0 + k) * hop);
    [this.r0, this.r1] = d.run;
    [this.s0, this.s1] = d.s;
    this.w = this.t.map((t) => smooth(Math.min(1, Math.max(0, (t - this.r0) / EDGE), Math.max(0, (this.r1 - t) / EDGE))));
    this.E = this.e.map((e) => 2 ** (e / 12) - 1);
    this.vp = d.vp ?? null; this.vn = d.vn ?? null;
  }
  get dur() { return Math.max(this.s1 - this.s0, 0); }
}

class Material {
  constructor(segs, hop) {
    this.hop = hop;
    this.segs = segs.map((d) => new Seg(d, hop));
    this.byNote = new Map();
    this.segs.forEach((s, k) => { if (!this.byNote.has(s.n)) this.byNote.set(s.n, []); this.byNote.get(s.n).push(k); });
    this.C = new Map();
    for (const [n, ks] of this.byNote) {
      let sum = 0; let cnt = 0;
      for (const k of ks) for (const e of this.segs[k].e) { sum += e; cnt++; }
      this.C.set(n, cnt ? sum / cnt : 0);
    }
  }
  has(n) { return this.byNote.has(n); }
  prev(k) {
    const s = this.segs[k];
    if (s.p == null || k === 0) return null;
    const o = this.segs[k - 1];
    return o.n === s.p && o.r0 === s.r0 && o.r1 === s.r1 ? k - 1 : null;
  }
  next(k) {
    const s = this.segs[k];
    if (s.x == null || k + 1 >= this.segs.length) return null;
    const o = this.segs[k + 1];
    return o.n === s.x && o.r0 === s.r0 && o.r1 === s.r1 ? k + 1 : null;
  }
  neighbours(n) {
    const out = [];
    for (const k of this.byNote.get(n) || []) { const s = this.segs[k]; if (s.p != null) out.push(s.p); if (s.x != null) out.push(s.x); }
    return out;
  }
}

/** 計画の strength → { 材料の鍵: Material }。 */
export function parseMaterials(strength) {
  const out = {};
  for (const [k, v] of Object.entries(strength?.materials || {})) out[k] = new Material(v, strength.hop);
  return out;
}

const dOf = (E, [ac, as, C]) => as * E + (ac - as) * (2 ** (C / 12) - 1);
const linOf = (e, [ac, as, C]) => as * e + (ac - as) * C;
const halfOf = (a, b) => Math.min(HALF, a.dur / 3, b.dur / 3);

function baseValues(sg, own, prev = null, nxt = null, hp = 0, hn = 0) {
  return sg.t.map((t, i) => {
    const bp = prev && hp > 0 ? smooth(((sg.s0 + hp) - t) / (2 * hp)) : 0;
    const bn = nxt && hn > 0 ? smooth((t - (sg.s1 - hn)) / (2 * hn)) : 0;
    const bo = 1 - bp - bn;
    let D = bo * dOf(sg.E[i], own); let L = bo * linOf(sg.e[i], own);
    if (prev) { D += bp * dOf(sg.E[i], prev); L += bp * linOf(sg.e[i], prev); }
    if (nxt) { D += bn * dOf(sg.E[i], nxt); L += bn * linOf(sg.e[i], nxt); }
    const F = 1 + sg.w[i] * D;
    return F >= F_MIN ? sg.m0[i] + 12 * Math.log2(Math.max(F, F_MIN)) : sg.m0[i] + sg.w[i] * L;
  });
}

function splitValue(mat, ka, kb, ra, rb) {
  const a = mat.segs[ka]; const b = mat.segs[kb];
  const h = halfOf(a, b);
  const va = baseValues(a, ra, null, rb, 0, h).at(-1);
  const vb = baseValues(b, rb, ra, null, h, 0)[0];
  const ta = a.t.at(-1); const tb = b.t[0];
  if (tb - ta <= 1e-12) return vb;
  const u = Math.min(1, Math.max(0, (a.s1 - ta) / (tb - ta)));
  return va + (vb - va) * u;
}

function ramp(num, den) {
  if (den <= 1e-12) return num >= den - 1e-12 && num > 0 ? 1 : 0;
  return num > 0 ? smooth(num / den) : 0;
}

/** 区切り k の鉛筆の点 [[秒, MIDI], ...]。role(ノート) = [ac, as]（混ぜる）か null（その今の値へつなぐ）。 */
export function segPoints(mat, k, role) {
  const sg = mat.segs[k];
  const [ac, as] = role(sg.n);
  const own = [ac, as, mat.C.get(sg.n)];
  const kp = sg.p != null ? mat.prev(k) : null;
  const kn = sg.x != null ? mat.next(k) : null;
  const rp = kp != null ? role(sg.p) : null;
  const rn = kn != null ? role(sg.x) : null;
  const prev = rp ? [rp[0], rp[1], mat.C.get(sg.p)] : null;
  const nxt = rn ? [rn[0], rn[1], mat.C.get(sg.x)] : null;
  const hp = prev ? halfOf(mat.segs[kp], sg) : 0;
  const hn = nxt ? halfOf(sg, mat.segs[kn]) : 0;
  const v = baseValues(sg, own, prev, nxt, hp, hn);
  const t = sg.t;
  const out = v.slice();
  const lock = Math.min(LOCK, sg.dur / 3);
  let head = null; let tail = null;
  if (sg.p != null) {
    if (prev) head = splitValue(mat, kp, k, prev, own);
    else if (sg.vp != null) {
      head = sg.vp;
      const den = (sg.s0 + lock) - t[0];
      for (let i = 0; i < t.length; i++) out[i] += ramp((sg.s0 + lock) - t[i], den) * (head - v[0]);
    }
  }
  if (sg.x != null) {
    if (nxt) tail = splitValue(mat, k, kn, own, nxt);
    else if (sg.vn != null) {
      tail = sg.vn;
      const den = t.at(-1) - (sg.s1 - lock);
      for (let i = 0; i < t.length; i++) out[i] += ramp(t[i] - (sg.s1 - lock), den) * (tail - v.at(-1));
    }
  }
  let pts = t.map((x, i) => [x, out[i]]);
  if (head != null) { pts = pts.filter((p) => Math.abs(p[0] - sg.s0) >= 1e-9); pts.unshift([sg.s0, head]); }
  if (tail != null) { pts = pts.filter((p) => Math.abs(p[0] - sg.s1) >= 1e-9); pts.push([sg.s1, tail]); }
  return pts.map(([a, b]) => [r6(a), r6(b)]);
}

/** 作り直すノート Map(ノート → [材料の鍵, ac, as])。center / shape = null はその割合を変えない（`strength.decide`）。 */
export function decide(notes, mats, { center = null, shape = null, includeManual = false, restart = [] } = {}) {
  const made = new Map();
  if (center == null && shape == null) return made;
  const rs = new Set(restart || []);
  for (const [nid, r] of Object.entries(notes || {})) {
    if (!r.sel) continue;
    if (r.manual && !includeManual) continue;
    if (r.state === 'ok') {
      const c = center == null ? r.center : center;
      const s = shape == null ? r.shape : shape;
      if (Math.abs(c - r.center) > 1e-9 || Math.abs(s - r.shape) > 1e-9) made.set(nid, [r.src, c, s]);
    } else if (r.state === 'none' || (r.state === 'stale' && rs.has(nid))) {
      const c = center == null ? 0 : center;
      const s = shape == null ? 0 : shape;
      if ((Math.abs(c) > 1e-9 || Math.abs(s) > 1e-9) && mats.new?.has(nid)) made.set(nid, ['new', c, s]);
    }
  }
  for (const [nid, [key]] of [...made]) {
    if (key === 'new' || !mats[key]) continue;
    for (const m of mats[key].neighbours(nid)) {
      const r = notes[m];
      if (made.has(m) || !r) continue;
      if (r.state === 'ok' && r.src === key && !r.manual) made.set(m, [key, r.center, r.shape]);
    }
  }
  return made;
}

function roles(notes, made, key) {
  return (nid) => {
    const m = made.get(nid);
    if (m) return m[0] === key ? [m[1], m[2]] : null;
    const r = notes[nid];
    if (r && r.state === 'ok' && r.src === key && !r.manual) return [r.center, r.shape];
    return null;
  };
}

/** 割合を当てたときに作り直すノートのフレーム → MIDI（鉛筆の点のうちフレームの時刻のもの）。
 *  hop の番号で返す（`Math.round(秒 / hop)`）。base = 作り直さないときの値（記録の割合の線・新しく合わせるノートは補正前）。 */
export function strengthFrames(strength, mats, params) {
  const notes = strength?.notes || {};
  const made = params ? decide(notes, mats, params) : new Map();
  const out = new Map();
  for (const [nid, [key, ac, as]] of made) {
    const mat = mats[key];
    const role = roles(notes, made, key);
    for (const k of mat.byNote.get(nid) || []) {
      if (key !== 'new' && Math.abs(ac) < 1e-9 && Math.abs(as) < 1e-9) {     // 線を外す → 補正前
        const sg = mat.segs[k];
        sg.t.forEach((t, i) => out.set(Math.round(t / mat.hop), { v: sg.m0[i], n: nid }));
      } else put(out, mat, k, role, nid);
    }
  }
  return { made, values: out };
}

function put(out, mat, k, role, nid) {
  const sg = mat.segs[k];
  const pts = new Map(segPoints(mat, k, role).map(([a, b]) => [r6(a), b]));
  for (const t of sg.t) {
    const v = pts.get(r6(t));
    if (v != null) out.set(Math.round(t / mat.hop), { v, n: nid });
  }
}

/** 作り直す前の値（プレビューの差の基準）: 記録のあるノートは記録の割合の線、新しく合わせるノートは補正前（m0）。 */
export function baseFrames(strength, mats, nid, key) {
  const out = new Map();
  const mat = mats[key];
  if (!mat) return out;
  if (key === 'new') {
    for (const k of mat.byNote.get(nid) || []) {
      const sg = mat.segs[k];
      sg.t.forEach((t, i) => out.set(Math.round(t / mat.hop), { v: sg.m0[i], n: nid }));
    }
    return out;
  }
  const notes = strength.notes || {};
  const r = notes[nid];
  const base = roles(notes, new Map(), key);
  const role = (m) => (m === nid ? [r.center, r.shape] : base(m));
  for (const k of mat.byNote.get(nid) || []) put(out, mat, k, role, nid);
  return out;
}

/** ポップアップの今と、すでに当てた割合（params0。null = まだ当てていない）の差のフレーム → { d: 半音, n: ノート }。
 *  view0(フレーム) = ポップアップを開いたときの線（無ければ null）。作り直さないときの値に使う。
 *  合わせた後に手で直していないノートでは、それが記録の割合の線そのもの（作ったときの隣の扱いも入っている）。
 *  手で直したノート・view0 の無いフレームは、記録の割合から作り直した線（baseFrames）を使う。 */
export function strengthDelta(strength, mats, params, params0, view0 = null) {
  const a = strengthFrames(strength, mats, params);
  const b = strengthFrames(strength, mats, params0);
  const notes = strength?.notes || {};
  const base = new Map();
  const baseOf = (nid, key) => {
    const k = `${key}|${nid}`;
    if (!base.has(k)) base.set(k, baseFrames(strength, mats, nid, key));
    return base.get(k);
  };
  const keyOf = (nid) => a.made.get(nid)?.[0] ?? b.made.get(nid)?.[0];
  const out = new Map();
  for (const i of new Set([...a.values.keys(), ...b.values.keys()])) {
    const nid = (a.values.get(i) || b.values.get(i)).n;
    const v0 = view0 && !notes[nid]?.manual ? view0(i) : null;
    const fb = Number.isFinite(v0) ? v0 : baseOf(nid, keyOf(nid)).get(i)?.v;
    const va = a.values.get(i)?.v ?? fb;
    const vb = b.values.get(i)?.v ?? fb;
    if (va == null || vb == null) continue;
    if (Math.abs(va - vb) > 1e-9) out.set(i, { d: va - vb, n: nid });
  }
  return out;
}

/** ポップアップに出す値: 選んだノートの割合の範囲。manual を含めないときは手で直したノートを除く。stale は除く。
 *  restart = 今の状態から合わせ直す stale のノート（0% から数える。変えられないノートには数えない）。 */
export function strengthSummary(strength, { includeManual = false, restart = [] } = {}) {
  const re = new Set(restart);
  const rows = Object.entries(strength?.notes || {}).filter(([, r]) => r.sel)
    .map(([n, r]) => [n, re.has(n) && r.state === 'stale' ? { ...r, state: 'none', reason: null } : r]);
  const use = rows.filter(([, r]) => r.state !== 'stale' && (includeManual || !r.manual));
  const one = (k) => {
    const vs = use.map(([, r]) => (r.state === 'ok' ? r[k] : 0));
    if (!vs.length) return { mixed: false, min: 0, max: 0, mean: 0 };
    const lo = Math.min(...vs); const hi = Math.max(...vs);
    return { mixed: hi - lo > 0.005, min: lo, max: hi, mean: vs.reduce((x, y) => x + y, 0) / vs.length };
  };
  const stale = rows.filter(([, r]) => r.state === 'stale');
  return {
    center: one('center'), shape: one('shape'),
    manual: rows.filter(([, r]) => r.manual).map(([n]) => n),
    stale: stale.map(([n]) => n),
    reasons: [...new Set(stale.map(([, r]) => r.reason).filter(Boolean))],
  };
}
