// 補正の度合いの色（issue #37。モック proposal/v4.html の A「連続」・常に表示）。
//
//   タイミング = ノートの帯、ピッチ = 線。補正なし = テイクの黄。
//   自動補正（ガイドに合わせる）は度合い 0〜1 に応じて 黄 → 橙 → 赤 へ連続的に（明るさも下がる。色覚への配慮）。
//   手動補正は白。自動と手動の両方がかかったときは最後にかけた方（エンジンが changeset の印から決める）。
//
// 度合い = 元からどれだけ動いたか ÷ 基準（エンジンの `project/correction.py`。基準は view data の
// `correction_ref`）。**ドラッグ中・「ガイドに合わせる」のスライダー中も同じ式で**今の見かけから測るので、
// ドラッグ中の色 = 離した後の色になる（端・移動のドラッグ・鉛筆・ピッチのドラッグ = 手動、ガイドに合わせる = 自動）。
import {
  COLORS, S, editedCurve, frameNoteIds, isMuted, noteFrames, pitchDelta, strokeData, warp,
} from './state.js';

// 黄（テイク）→ 橙 → 赤。段ごとに暗くなる（相対輝度 0.63 → 0.45 → 0.28 → 0.16）
export const RAMP = ['#e6d24a', '#eaa73c', '#e3702e', '#d23a2a'];
export const MANUAL = '#ffffff';
// 無音のノートのピッチの線（補正の色にしない。index.html の --fg3）
const MUTED_LINE = '#5c5c62';
const EPS_SEC = 1e-4;
const EPS_CENTS = 0.1;

const h2r = (h) => [1, 3, 5].map((k) => parseInt(h.slice(k, k + 2), 16));
const r2h = (c) => `#${c.map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, '0')).join('')}`;
const mix = (a, b, f) => { const x = h2r(a); const y = h2r(b); return r2h(x.map((v, k) => v + (y[k] - v) * f)); };

/** 度合い 0〜1 → 色（RAMP の間を線形に）。 */
export function degreeColor(a) {
  const f = Math.max(0, Math.min(1, a || 0)) * (RAMP.length - 1);
  const k = Math.min(RAMP.length - 2, Math.floor(f));
  return mix(RAMP[k], RAMP[k + 1], f - k);
}

/** 子音の区間の色: 同じ色相・明るさのまま彩度だけ落とす（v3 §6。テイクの黄なら COLORS.CONS）。 */
const desatCache = new Map();
export function desat(hex) {
  if (hex === COLORS.TAKE) return COLORS.CONS;
  if (desatCache.has(hex)) return desatCache.get(hex);
  const [r, g, b] = h2r(hex).map((v) => v / 255);
  const mx = Math.max(r, g, b); const mn = Math.min(r, g, b); const l = (mx + mn) / 2; const d = mx - mn;
  let out = hex;
  if (d) {
    const s = d / (1 - Math.abs(2 * l - 1));
    let h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    h *= 60;
    if (h < 0) h += 360;
    const c = (1 - Math.abs(2 * l - 1)) * s * 0.45; const x = c * (1 - Math.abs(((h / 60) % 2) - 1)); const m = l - c / 2;
    const [a1, b1, c1] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
    out = r2h([a1 + m, b1 + m, c1 + m].map((v) => v * 255));
  }
  desatCache.set(hex, out);
  return out;
}

function ref() {
  const r = S.vd?.correction_ref || {};
  return { t: r.timing_ms || 80, p: r.pitch_cents || 100 };
}

/** 昇順の配列 xs で x 以上の最初の位置。 */
function lower(xs, x) {
  let lo = 0; let hi = xs.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (xs[m] < x) lo = m + 1; else hi = m; }
  return lo;
}

/** ノートのタイミングを測る点 [[編集前の秒, 表示中の編集後の秒, side]]: 頭・尻・中の時間写像の節・
 * 計画の節・音素境界（エンジンの correction.timing_shift と同じ点。計画・境界のドラッグで新しくできる節も含む）。 */
function timingPoints(n) {
  const a = n.start_sec; const b = n.end_sec;
  const pts = [[a, n.edited_start_sec, 'right'], [b, n.edited_end_sec, 'left']];
  const tm = S.vd?.time_map;
  if (tm?.src_sec?.length) {
    const xs = tm.src_sec;
    for (let k = lower(xs, a); k < xs.length && xs[k] <= b; k++) {
      if (xs[k] > a && xs[k] < b) pts.push([xs[k], tm.out_sec[k], 'right']);
    }
  }
  const ks = S.plan?.data?.knots;
  if (ks) {
    const x0 = S.plan.x0 || 0;
    for (const [src, cur, d, side] of ks) {
      if (src > a && src < b) pts.push([src, cur + d * x0, side === 'l' ? 'left' : 'right']);
    }
  }
  if (S.local.btime.size) {
    for (const bd of S.bounds) if (bd.sec > a && bd.sec < b) pts.push([bd.sec, bd.edited_sec, 'right']);
  }
  return pts;
}

/** タイミングの補正（ドラッグ中の見かけを含む）: { degree, manual, amountMs } か null。 */
export function timingCorr(n) {
  const eng = n.timing_corr ? { degree: n.timing_corr.degree, manual: !!n.timing_corr.manual,
    amountMs: n.timing_corr.amount_ms } : null;
  if (!S.plan && !S.local.btime.size) return eng;
  let changed = false; let amount = 0;
  for (const [src, disp, side] of timingPoints(n)) {
    if (disp == null) continue;
    const v = warp(disp, side);
    if (Math.abs(v - disp) > EPS_SEC) changed = true;
    amount = Math.max(amount, Math.abs(v - src));
  }
  if (!changed) return eng;
  if (amount <= EPS_SEC) return null;
  const ms = amount * 1000;
  return { degree: Math.min(1, ms / ref().t), manual: S.plan?.data?.kind !== 'guide', amountMs: ms };
}

/** 鉛筆で描いている線がこのノートにかかるか。 */
function strokeOn(n) {
  if (!S.stroke) return false;
  const sd = strokeData();
  const r = noteFrames().get(n.id);
  return !!(sd && r && sd.hi >= r[0] && sd.lo <= r[1]);
}

/** ピッチの補正（ドラッグ中の見かけを含む）: { degree, manual, amountCents } か null。 */
export function pitchCorr(n) {
  const pc = n.pitch_corr;
  const eng = pc ? { degree: pc.degree, manual: !!pc.manual, amountCents: pc.amount_cents } : null;
  if (n.kind !== 'note') return null;
  const d = pitchDelta(n.id);
  const st = strokeOn(n);
  const pl = S.plan;
  let shape = false;
  if (pl?.data?.params?.match_pitch_shape && Math.abs((pl.pitch || 0) - (pl.pitch0 || 0)) > 1e-9) {
    const [a, b] = noteFrames().get(n.id) || [0, -1];
    const ids = frameNoteIds();
    const base = S.vd.f0.take_edited_midi;
    for (let i = a; i <= b; i++) {
      const frame = pl.shapeFrames?.get(i);
      if (ids[i] !== n.id || base[i] == null || !frame) continue;
      const [h0, h1, w] = frame;
      const hz = (strength) => h0 + strength * w * (h1 - h0);
      if (Math.abs(12 * Math.log2(hz(pl.pitch || 0) / hz(pl.pitch0 || 0))) > 1e-6) {
        shape = true;
        break;
      }
    }
  }
  if (Math.abs(d) < 1e-9 && !st && !shape) return eng;
  let amount = Math.abs((n.cents || 0) + d * 100);
  if (st || shape) {
    const [a, b] = noteFrames().get(n.id) || [0, 0];
    const curve = editedCurve();
    const source = S.vd.f0.take_midi;
    for (let i = a; i <= b; i++) {
      if (curve[i] != null && source[i] != null)
        amount = Math.max(amount, Math.abs(curve[i] - source[i]) * 100);
    }
  }
  if (!st && !shape && amount <= EPS_CENTS && !pc?.shape) return null;
  // 手動: ピッチのドラッグ・鉛筆。自動: 「ガイドに合わせる」のスライダー（計画のピッチ）
  const manual = st || (S.local.pitch.get(n.id) || 0) !== 0;
  return { degree: Math.min(1, amount / ref().p), manual, amountCents: amount };
}

/** 補正 → 色（無ければテイクの黄）。 */
export function corrColor(c) {
  if (!c) return COLORS.TAKE;
  return c.manual ? MANUAL : degreeColor(c.degree);
}
export const bandColor = (n) => corrColor(timingCorr(n));
export const lineColor = (n) => corrColor(pitchCorr(n));

/** フレームごとの線の色を引く関数（描き直し 1 回ぶん。ノートごとに 1 度だけ測る）。 */
export function lineColorer() {
  const fn = frameNoteIds();
  const cache = new Map();
  return (i) => {
    const id = fn[i];
    if (!id) return COLORS.TAKE;
    let c = cache.get(id);
    if (c === undefined) {
      const n = S.byId.get(id);
      c = n && n.kind === 'note' ? (isMuted(n) ? MUTED_LINE : lineColor(n)) : COLORS.TAKE;
      cache.set(id, c);
    }
    return c;
  };
}
