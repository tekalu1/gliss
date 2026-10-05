// ピアノロールの描画（SVG）。モックの render() をそのまま持ってきて、
// 架空データのところだけ `export_view_data` の実データに差し替えたもの。
import {
  BLACK, COLORS, LAYOUT, PITCH_VIEW, S, bandOf, boundSec, boxOf, clamp, clampPitchView, editedCurve,
  currentTrack, fadeGain, fadeOf, fmtTime, guideBandOf,
  guideFrames, isMuted, isSel, noteFrames, noteName, phSpan, pitchOf, planConnChanges, sign, spanOf, totalSec,
  toEdited, trHalves, warp,
} from './state.js';
import { withKey } from './keys.js';
import { asrCandidate, asrRunning } from './asr.js';
import { syncAppMenu, undoLabels } from './commands.js';
import { guideShown } from './session.js';
import {
  G, GRID_BARS, GRID_SEC, barsMode, currentDiv, pitchSnapOn, snapStep, ticks,
} from './grid.js';
import { renderTempo } from './tempo.js';
import { bandColor, desat, lineColorer } from './corr.js';
import { araEditorHead } from './ara.js';

const { KEYS_W, SCALE_H, LANE_H, EDGE } = LAYOUT;
const { TAKE, GUIDE, SEL, WAS, AI: AI_EDGE } = COLORS;

let svg = null;
let W = 1200;
let H = 396;
// 描き直し・再生位置の移動のたびに呼ぶもの（トラックビュー。上下で白枠・再生位置・ループを合わせる）
const hooks = { render: [], head: [] };
export function onRender(fn) { hooks.render.push(fn); }
export function onPlayhead(fn) { hooks.head.push(fn); }

export function attach(el) { svg = el; }
export function size() { return { W, H }; }

export function rollTop() { return SCALE_H; }
export function rollBottom() { return H - LANE_H; }
export function rollHeight() { return rollBottom() - rollTop(); }
// 縦は S.pv（top = 上の端の音程、span = 見えている半音の数）。縦ズーム（既定 Ctrl+ホイール）で拡大縮小、縦スクロール（既定 ホイール）でスクロール（issue #27）
const pv = () => S.pv || { top: S.midiHi + 0.5, span: S.midiHi - S.midiLo + 1 };
export function rowH() { return rollHeight() / pv().span; }

export function X(t) { return KEYS_W + (t - S.view.t0) / S.view.span * (W - KEYS_W); }
export function T(x) { return S.view.t0 + (x - KEYS_W) / (W - KEYS_W) * S.view.span; }
export function Y(m) { return rollTop() + (pv().top - m) * rowH(); }
export function M(y) { return pv().top - (y - rollTop()) / rowH(); }

const f1 = (v) => (Math.round(v * 10) / 10).toString();

// ---------------------------------------------------------------- 小道具
function tip(x, y, text, anchor) {
  const w = text.length * 6.6 + 10;
  const rx = anchor === 'middle' ? x - w / 2 : x;
  return `<g pointer-events="none"><rect x="${f1(rx)}" y="${f1(y - 9)}" width="${f1(w)}" height="17" rx="2" fill="#232326"/>`
    + `<text x="${f1(anchor === 'middle' ? x : x + 5)}" y="${f1(y + 4)}" font-size="11" fill="${SEL}" text-anchor="${anchor === 'middle' ? 'middle' : 'start'}">${text}</text></g>`;
}

function esc(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}

/** ピアノロールの 1 秒の px。 */
export function pps() { return (W - KEYS_W) / S.view.span; }
const GRID_MIN_PX = 22;        // 線の間隔の下限（モックと同じ）
/** エディターの時間スナップの刻み（秒。グリッドの細かさ・自動ならズームに合わせた線の刻み）。 */
export function edStep() { return snapStep(pps(), GRID_MIN_PX); }
// グリッドの線（小節・拍・それより細かい）。帯の色より暗く、背景の半音の行と見分けられる程度（モックと同じ）
const GRIDC = ['#35353b', '#29292e', '#212125'];

/** フェードのあるノートの点列: フェードの区間に 2 px ごとの点を足し、帯の太さに音量の倍率を掛ける（v3 §5）。 */
function applyFadeShape(n, pts, span) {
  const { fi, fo } = fadeOf(n);
  if ((fi <= 0 && fo <= 0) || pts.length < 2) return pts;
  const xa = X(span[0] + fi); const xb = X(span[1] - fo);
  const inFade = (x) => (fi > 0 && x < xa) || (fo > 0 && x > xb);
  const out = [];
  for (let k = 0; k < pts.length; k++) {
    const p = pts[k];
    out.push(p);
    const q = pts[k + 1];
    if (!q || q.x - p.x <= 2 || !(inFade(p.x) || inFade(q.x))) continue;
    const m = Math.min(200, Math.floor((q.x - p.x) / 2));
    for (let j = 1; j < m; j++) {
      const u = j / m;
      out.push({ x: p.x + (q.x - p.x) * u, y: p.y + (q.y - p.y) * u, h: p.h + (q.h - p.h) * u });
    }
  }
  for (const p of out) p.h *= fadeGain(n, T(p.x), span);
  return out;
}

// ---------------------------------------------------------------- ノートごとの小さな波形（blob）
// Melodyne の blob と同じ見せ方（v3 §3）: **ノートの平均の音程**（`band_midi`。音量で重み付け、
// 無声・子音を除く）に水平な帯で置き、音量の包絡（`f0.take_env` / `guide_env`）の太さで上下に広げる。
// ピッチの揺れは細い線（黄色の曲線）で重ねる。しゃくり・ビブラートで線が帯の外に出るのはそのまま見せる。
// 端は縦の直線になるので、つかむ所が見える。四角い枠は描かない（当たり判定は透明な矩形で別に持つ）。
// 選択・ホバーは塗りの濃さだけで見せる（`index.html` の `.nb` / `.sel`）。

/** 包絡 0〜1 → 片側の太さ（px）。小さい音も見えるように平方根で持ち上げる（無音は 0 = 描かない）。 */
function blobHalf(env, hmax) {
  const e = env == null ? 0 : clamp(env, 0, 1);
  return hmax * Math.sqrt(e);
}
/** いちばん大きい音の片側の太さ（px）。半音の行の 8 割（大きい音で全体 1.6 行。Melodyne の blob くらい）。 */
function blobHMax() { return clamp(rowH() * 0.8, 3, 14); }

/** 点列 [{x, y, h}] → 上の縁を行き、下の縁を戻る閉じた path。 */
function blobD(pts) {
  if (pts.length < 2) return '';
  let d = '';
  for (let k = 0; k < pts.length; k++) d += `${k ? 'L' : 'M'}${f1(pts[k].x)} ${f1(pts[k].y - pts[k].h)}`;
  for (let k = pts.length - 1; k >= 0; k--) d += `L${f1(pts[k].x)} ${f1(pts[k].y + pts[k].h)}`;
  return `${d}Z`;
}
/** 区間ごとの点列 → 1 本の path（区間ごとに閉じる）。 */
function blobsD(segs) { return segs.map(blobD).join(''); }

// ---- 子音の色（v3 §6）: 歌詞があるときだけ、帯の中の子音の区間を同じ色相のまま彩度だけ落とす
/** 子音の区間の横の範囲 [[左, 右], ...]（px。ドラッグ中の見かけを含む）。歌詞が無ければ null。 */
function consonantXs() {
  if (!S.ph) return null;
  const out = [];
  for (const p of S.ph.phonemes || []) {
    if (p.label !== 'consonant') continue;
    const [a, b] = phSpan(p);
    const xa = X(a); const xb = X(b);
    if (xb < KEYS_W - 20 || xa > W + 20 || xb <= xa) continue;
    out.push([xa, xb]);
  }
  return out.length ? out : null;
}

/** 点列を子音の範囲で切り分ける → { main: [点列...], cons: [点列...] }（境目の点は両方に入れる）。 */
function splitCons(pts, xs) {
  if (!xs || pts.length < 2) return { main: [pts], cons: [] };
  const lo = pts[0].x; const hi = pts[pts.length - 1].x;
  const rs = xs.filter(([a, b]) => b > lo && a < hi);
  if (!rs.length) return { main: [pts], cons: [] };
  const inCons = (x) => rs.some(([a, b]) => x >= a && x < b);
  const cuts = rs.flat();
  const out = { main: [], cons: [] };
  let cur = [pts[0]];
  let flag = null;
  for (let k = 0; k + 1 < pts.length; k++) {
    const p = pts[k]; const q = pts[k + 1];
    const xs2 = [p.x, ...cuts.filter((c) => c > p.x && c < q.x).sort((a, b) => a - b), q.x];
    for (let j = 0; j + 1 < xs2.length; j++) {
      const f = inCons((xs2[j] + xs2[j + 1]) / 2);
      const u = q.x > p.x ? (xs2[j + 1] - p.x) / (q.x - p.x) : 1;
      const np = j + 2 === xs2.length ? q : { x: xs2[j + 1], y: p.y + (q.y - p.y) * u, h: p.h + (q.h - p.h) * u };
      if (flag === null) flag = f;
      if (f !== flag) {
        if (cur.length >= 2) out[flag ? 'cons' : 'main'].push(cur);
        cur = [cur[cur.length - 1]];
        flag = f;
      }
      cur.push(np);
    }
  }
  if (cur.length >= 2) out[flag ? 'cons' : 'main'].push(cur);
  return out;
}

/** 区間ごとの点列 → { main: d, cons: d }（子音の範囲 xs が無ければ全部 main）。 */
function blobsSplit(segs, xs) {
  const m = []; const c = [];
  for (const seg of segs) {
    const r = splitCons(seg, xs);
    m.push(...r.main);
    c.push(...r.cons);
  }
  return { main: blobsD(m), cons: blobsD(c) };
}

// ノートの区切り（枠の代わり）: blob の両端を細めて、隣と接していても切れ目が見えるようにする
// （Melodyne の blob が 1 つずつ形で切れているのと同じ読み方。色・線は足さない）
const PINCH_PX = 4;           // 端から細め始める幅（px。ノートが短いときは 1/4 まで）
const PINCH_MIN = 0.15;       // 端での太さ（元の太さに対する割合）

/** 点列の両端を細める（その場で書き換える）。 */
function pinchEnds(pts) {
  if (pts.length < 2) return pts;
  const xa = pts[0].x; const xb = pts[pts.length - 1].x;
  const w = Math.min(PINCH_PX, (xb - xa) / 4);
  if (w <= 0.2) return pts;
  for (const p of pts) {
    const d = Math.min(p.x - xa, xb - p.x);
    if (d >= w) continue;
    const u = Math.max(0, d / w);
    p.h *= PINCH_MIN + (1 - PINCH_MIN) * Math.sin(u * Math.PI / 2);
  }
  return pts;
}

/** 1 px より近い点を間引く（曲全体を表示したとき、フレーム数ぶんの点を描かない）。両端は残す。 */
function thin(pts) {
  if (pts.length < 3) return pts;
  const out = [pts[0]];
  for (let k = 1; k < pts.length - 1; k++) {
    const q = out[out.length - 1];
    if (pts[k].x - q.x >= 1) out.push(pts[k]);
    else if (pts[k].h > q.h) out[out.length - 1] = { ...q, h: pts[k].h };   // 間引いても山は残す
  }
  out.push(pts[pts.length - 1]);
  return out;
}

// 音程の無い区間（息・囁き・無声の子音）は、実際に音があるところだけ描く。
//   包絡 0.03（大きい歌声の −30 dB）未満は無音とみなす。C の中央の無音は −52〜−56 dBFS で
//   歌声（−13 dBFS）の約 −40 dB。息・子音は −25 dB 前後以上。
//   60 ms 未満しか続かないものも描かない（隣のノートの立ち上がり・余韻が 20 ms の窓に漏れた分）。
const NOPITCH_MIN_ENV = 0.03;
const NOPITCH_MIN_SEC = 0.06;

/** テイクのノートの blob（区間ごとの点列の配列。ドラッグ中の見かけ＝計画・ピッチの差分を含む）。 */
function takeBlobs(n, hmax, ctx) {
  const r = noteFrames().get(n.id);
  if (!r) return [];
  const [s0, s1] = spanOf(n);
  const xa = X(s0); const xb = X(s1);
  if (xb < KEYS_W - 20 || xa > W + 20) return [];
  const f0 = S.vd.f0;
  const env = f0.take_env;
  const es = f0.take_edited_sec;
  const pt = (i, y) => ({ x: X(warp(es[i])), y, h: blobHalf(env ? env[i] : 0.5, hmax) });
  if (n.kind === 'note') {
    const y = Y(bandOf(n));
    const pts = [];
    for (let i = r[0]; i <= r[1]; i++) pts.push(pt(i, y));
    // 両端はノートの端まで（隣と接していれば blob どうしも接する。区切りは pinchEnds で見せる）
    if (pts.length && xa < pts[0].x) pts.unshift({ ...pts[0], x: xa });
    if (pts.length && xb > pts[pts.length - 1].x) pts.push({ ...pts[pts.length - 1], x: xb });
    return [thin(pinchEnds(applyFadeShape(n, pts, [s0, s1])))];
  }
  // 音程の無い区間: 音がある（しきい値以上が 60 ms 以上続く）ところだけ、近い側の隣のノートの高さに平らに
  // （頭側 = 前のノートの余韻、尻側 = 次のノートの前の息・子音）
  const lv = noPitchLevel(n, ctx);
  if (n.kind === 'silence') {
    const y = Y(lv.pa ?? lv.pb ?? lv.median);
    return [[{ x: xa, y, h: 3 }, { x: xb, y, h: 3 }]];
  }
  if (!env) return [];
  const minRun = Math.min(Math.max(1, Math.round(NOPITCH_MIN_SEC / f0.hop_sec)),
    Math.max(1, r[1] - r[0] + 1));
  const out = [];
  let run = [];
  const flush = () => {
    if (run.length >= minRun) {
      const mid = (run[0] + run[run.length - 1]) / 2;
      const head = mid - r[0] < r[1] - mid;
      const m = (head ? lv.pa ?? lv.pb : lv.pb ?? lv.pa) ?? lv.median;
      out.push(thin(pinchEnds(run.map((i) => pt(i, Y(m))))));
    }
    run = [];
  };
  for (let i = r[0]; i <= r[1]; i++) {
    if (env[i] != null && env[i] >= NOPITCH_MIN_ENV) run.push(i); else flush();
  }
  flush();
  // 無音と、分割後に短くなった低音量の区間にも操作できる細い帯を残す。
  if (!out.length) {
    const y = Y(lv.pa ?? lv.pb ?? lv.median);
    out.push([{ x: xa, y, h: 3 }, { x: xb, y, h: 3 }]);
  }
  return out;
}

const NO_PITCH_NEAR = 0.6;       // 音程の無い区間の高さは、この秒数以内の隣のノートに合わせる
/** 描き直し 1 回ぶんの下ごしらえ（ノートの並びの位置と、全体の中央値）。 */
function noPitchCtx() {
  const ps = S.pitched.map(bandOf).sort((a, b) => a - b);
  return {
    idx: new Map(S.notes.map((m, i) => [m.id, i])),
    median: ps.length ? ps[ps.length >> 1] : (S.midiLo + S.midiHi) / 2,
  };
}
/** 音程の無い区間の隣のノートの高さ（MIDI）: { pa: 前のノートの帯, pb: 次のノートの帯, median }。
 * 0.6 秒より離れた隣は null（どちらも無ければ全体のノートの中央値に置く）。 */
function noPitchLevel(n, ctx) {
  const idx = ctx.idx.get(n.id);
  let pa = null; let pb = null;
  for (let k = idx - 1; k >= 0; k--) {
    const m = S.notes[k];
    if (n.start_sec - m.end_sec > NO_PITCH_NEAR) break;
    if (m.kind === 'note') { pa = bandOf(m); break; }
  }
  for (let k = idx + 1; k < S.notes.length; k++) {
    const m = S.notes[k];
    if (m.start_sec - n.end_sec > NO_PITCH_NEAR) break;
    if (m.kind === 'note') { pb = bandOf(m); break; }
  }
  return { pa, pb, median: ctx.median };
}

/** ガイドのノートの blob の点列（ガイドはテイクの時間に置いた位置。編集では動かない）。 */
function guideBlobPts(g, hmax) {
  const r = guideFrames().get(g.id);
  if (!r) return [];
  const f0 = S.vd.f0;
  const gs = f0.guide_sec; const env = f0.guide_env;
  if (X(g.end_sec) < KEYS_W - 20 || X(g.start_sec) > W + 20) return [];
  const y = Y(guideBandOf(g));
  const pts = [];
  for (let i = r[0]; i <= r[1]; i++) {
    pts.push({ x: X(gs[i]), y, h: blobHalf(env ? env[i] : 0.5, hmax) });
  }
  const xa = X(g.start_sec); const xb = X(g.end_sec);
  if (pts.length && xa < pts[0].x) pts.unshift({ ...pts[0], x: xa });
  if (pts.length && xb > pts[pts.length - 1].x) pts.push({ ...pts[pts.length - 1], x: xb });
  return thin(pinchEnds(pts));
}

// ---------------------------------------------------------------- 端のつかみ（v3 §3）
// 帯の端から内側 12 px・外側 4 px（短いノートは内側を幅の 1/3 まで）、縦は帯の太さによらず中心から ±14 px。
// 外側は隣のノートとの隙間の半分まで（隣の端・本体のつかみと重ねない。接していれば外側は無い）。
const EDGE_IN = 12;
const EDGE_OUT = 4;
const EDGE_Y = 14;

/** S.pitched[k] の端のつかみの横の範囲 [左, 右]（px）。 */
function edgeGrab(k, which, x0, x1, yc) {
  const inner = Math.min(EDGE_IN, Math.max(0, x1 - x0) / 3);
  const nb = S.pitched[which === 'start' ? k - 1 : k + 1];
  let outer = EDGE_OUT;
  if (nb) {
    const gap = which === 'start' ? x0 - X(spanOf(nb)[1]) : X(spanOf(nb)[0]) - x1;
    outer = clamp(gap / 2, 0, EDGE_OUT);
  }
  // 接した子音・息の端にも最低 4 px 残す。長い端つまみなら残りの 4 px を
  // 音程ノート側に使える（音程のない帯が間を埋めても外側が消えない）。
  const x = which === 'start' ? x0 : x1;
  for (const v of npEdgeXs) {
    if (Math.abs(v.x - x) < 1 && Math.abs(v.y - yc) < 2 * EDGE_Y)
      outer = Math.min(outer, v.ceded);
  }
  return which === 'start' ? [x0 - outer, x0 + inner] : [x1 - inner, x1 + outer];
}

// ---------------------------------------------------------------- 子音・息の幅とタイミング（issue #35）
// 音程の無いノート（息・無声の子音）も、描いている帯（音のあるところ）をつかんで横に動かし、ノートの端
// （帯の端ではなくノートの区切り。乗ると明るい縦線）をつかんで幅を変える。操作・接続・取り消しは音程のある
// ノートと同じ（エンジンの計画）。上下には動かない（音程が無いのでピッチの対象外。interact.js は横の移動だけにする）。
let npEdgeXs = [];            // 描き直し 1 回ぶん: 子音・息の端の位置とつかみ幅
const NP_Y = 6;               // 帯の当たりの縦の最小（中心から ±px）

/** 音程のない区間の当たり。はさみ・ミュートでは区間全体が対象。 */
function noPitchHit(n, segs, conn) {
  const [s0, s1] = spanOf(n);
  const xa = X(s0); const xb = X(s1);
  let lo = Infinity; let hi = -Infinity; let bx0 = Infinity; let bx1 = -Infinity;
  for (const pts of segs) {
    for (const p of pts) {
      lo = Math.min(lo, p.y - Math.max(p.h, NP_Y));
      hi = Math.max(hi, p.y + Math.max(p.h, NP_Y));
      bx0 = Math.min(bx0, p.x); bx1 = Math.max(bx1, p.x);
    }
  }
  if (!Number.isFinite(lo)) return '';
  if (S.tool === 'cut' || S.tool === 'mute') return `<rect data-note="${n.id}" x="${f1(xa)}" y="${f1(lo)}" width="${f1(Math.max(2, xb - xa))}" height="${f1(hi - lo)}" fill="transparent"/>`;
  let out = `<rect data-nop="${n.id}" x="${f1(bx0)}" y="${f1(lo)}" width="${f1(Math.max(2, bx1 - bx0))}" height="${f1(hi - lo)}" fill="transparent" style="cursor:move"/>`;
  const inner = Math.min(EDGE_IN, Math.max(0, xb - xa) / 3);
  for (const [which, x, pts] of [['start', xa, segs[0]], ['end', xb, segs[segs.length - 1]]]) {
    const yc = pts[which === 'start' ? 0 : pts.length - 1].y;
    const touchingNote = S.pitched.some((p) => {
      const edge = X(spanOf(p)[which === 'start' ? 1 : 0]);
      return Math.abs(edge - x) < 1 && Math.abs(Y(bandOf(p)) - yc) < 2 * EDGE_Y;
    });
    const ceded = touchingNote ? Math.min(EDGE_OUT, Math.max(0, inner - EDGE_OUT)) : 0;
    const [ha, hb] = which === 'start' ? [x + ceded, x + inner] : [x - inner, x - ceded];
    out += `<rect data-nop="${n.id}" data-nop-edge="${which}" x="${f1(ha)}" y="${f1(yc - EDGE_Y)}" width="${f1(Math.max(1, hb - ha))}" height="${2 * EDGE_Y}" fill="transparent" style="cursor:${conn[which] && !S.alt ? 'col-resize' : 'ew-resize'}"/>`;
    npEdgeXs.push({ x, y: yc, ceded });
  }
  return out;
}

/** 子音・息の端が隣と接しているか（接続の見込み。カーソルの形だけに使う。エンジンの既定と同じく接していれば接続）。 */
function noPitchConn(n) {
  const i = S.notes.indexOf(n);
  const a = S.notes[i - 1]; const b = S.notes[i + 1];
  const touch = (x, y) => !!x && !!y
    && Math.abs(y.start_sec - x.end_sec) < 1e-6;
  return { start: touch(a, n), end: touch(n, b) };
}

/** 子音・息の端をドラッグ中の接続の記号（計画の info。接続 = 塗りの点、切り離し = 両端に白抜きの点）。 */
function noPitchGlyph(n, which, x, y) {
  const info = S.plan?.data?.info;
  const dr = S.drag;
  if (!info?.pair || S.plan.data.kind !== 'edge' || S.plan.data.params?.note_id !== n.id) return '';
  let conn = !!info.connected;
  const d = S.plan.data;
  if (d.snap_x != null && Math.abs((S.plan.x || 0) - d.snap_x) < 1e-6 && Math.abs(S.plan.x || 0) > 1e-9) conn = true;
  const key = `${info.pair[0]}|${info.pair[1]}`;
  if (conn && !(dr?.alt && !dr.moved)) {
    return `<g data-conn="${key}" data-state="connected" pointer-events="none"><circle cx="${f1(x)}" cy="${f1(y)}" r="3.4" fill="${SEL}"/></g>`;
  }
  const nb = S.byId.get(info.neighbour);
  let s = `<g data-conn="${key}" data-state="${conn ? 'cut' : 'detached'}" pointer-events="none">`
    + `<circle cx="${f1(x)}" cy="${f1(y)}" r="3.2" fill="#111113" stroke="${SEL}" stroke-width="1.3"/>`;
  if (nb) {
    const nx = X(which === 'start' ? spanOf(nb)[1] : spanOf(nb)[0]);
    const ny = nb.kind === 'note' ? Y(bandOf(nb)) : y;
    s += `<circle cx="${f1(nx)}" cy="${f1(ny)}" r="3.2" fill="#111113" stroke="${SEL}" stroke-width="1.3"/>`;
  }
  return `${s}</g>`;
}

/** 端の明るい縦線の片側の長さ（px）: 端から 30 ms 内側の帯の太さ（最低 8 px）。 */
function edgeHalf(n, which, hmax) {
  const r = noteFrames().get(n.id);
  const env = S.vd.f0.take_env;
  if (!r || !env) return 8;
  const k = Math.round(0.03 / S.vd.f0.hop_sec);
  const i = which === 'start' ? Math.min(r[1], r[0] + k) : Math.max(r[0], r[1] - k);
  return Math.max(8, blobHalf(env[i], hmax));
}

/** ノートの帯のいちばん太いところの片側（px）。フェードのつまみの高さに使う。 */
function noteHalfMax(n, hmax) {
  const r = noteFrames().get(n.id);
  const env = S.vd.f0.take_env;
  if (!r || !env) return hmax * 0.7;
  let m = 0;
  for (let i = r[0]; i <= r[1]; i++) if (env[i] != null && env[i] > m) m = env[i];
  return blobHalf(m, hmax);
}

/** テスト用: フェードのつまみ（いま描いているもの。中心の px）。 */
export function fadeInfo(id) {
  return [...(svg?.querySelectorAll(`rect[data-fade][data-note="${id}"]`) || [])].map((r) => ({
    side: r.dataset.fade, x: +r.getAttribute('x') + 3.5, y: +r.getAttribute('y') + 3.5,
  }));
}

/** テスト用: 端のつかみの矩形と、明るい縦線（いま描いているもの）。 */
export function edgeInfo(id) {
  const q = (sel) => svg?.querySelector(sel);
  const box = (el) => (el ? { x: +el.getAttribute('x'), y: +el.getAttribute('y'),
    w: +el.getAttribute('width'), h: +el.getAttribute('height') } : null);
  const n = S.byId.get(id);
  const hot = [...(svg?.querySelectorAll('[data-edge-hot]') || [])].map((l) => ({
    which: l.dataset.edgeHot, x: +l.getAttribute('x1'), y1: +l.getAttribute('y1'), y2: +l.getAttribute('y2'),
  }));
  return {
    start: box(q(`rect[data-note="${id}"][data-edge="start"]`)),
    end: box(q(`rect[data-note="${id}"][data-edge="end"]`)),
    x0: n ? X(spanOf(n)[0]) : null, x1: n ? X(spanOf(n)[1]) : null,
    yc: n ? Y(bandOf(n)) : null,
    hot,
  };
}

// ---------------------------------------------------------------- 接続（B 案: 触れた境目だけノードとエッジ）
// 普段は何も出さない。境目に近づいたとき・ノートを選んだとき・ドラッグ中だけ、その境目に
//   接続 = 塗りの点＋線（線の曲がる幅 = なだらかさの窓。段差なら縦線、なだらかなら S 字）
//   切り離し = 両側の端に白抜きの点
// を選択色（白）だけで描く。Alt を押すと接続の線が破線・点が白抜き（ここで切れる予告）。
// 吸着の位置まで伸ばすと、離した後と同じく実線でつながる。
const NEAR_PX = 28;           // 境目に「近づいた」とみなす横の距離
const pairKey = (a, b) => `${a.id}|${b.id}`;

/** 記号を出す境目 → Set('a|b')。ドラッグ中はその境目、そうでなければ近づいた境目、と選択ノートの両側。 */
function connFocus() {
  const out = new Set();
  if (S.tool !== 'main' || !S.vd) return out;
  const P = S.pitched;
  const idx = new Map(P.map((n, i) => [n.id, i]));
  const sides = (id) => {
    const i = idx.get(id);
    if (i == null) return;
    if (i > 0) out.add(pairKey(P[i - 1], P[i]));
    if (i < P.length - 1) out.add(pairKey(P[i], P[i + 1]));
  };
  const dr = S.drag;
  if (dr && dr.type === 'edge') {
    const i = idx.get(dr.id);
    if (i != null) {
      if (dr.which === 'end' && i < P.length - 1) out.add(pairKey(P[i], P[i + 1]));
      if (dr.which === 'start' && i > 0) out.add(pairKey(P[i - 1], P[i]));
    }
  } else if (dr && dr.type === 'note') {
    for (const id of dr.ids) sides(id);
  } else if (!dr && S.near) {
    out.add(S.near);
  }
  for (const id of S.sel) sides(id);
  return out;
}
/** 記号をいま描いているか（Alt でメニューバーを止めるのはこのときだけ。選択ノートが画面の外なら描かない）。 */
export function hasConnFocus() { return connGlyphs() !== ''; }

/** ポインタ（px）に近い境目の 'a|b'（無ければ null）。隙間の真ん中など、どちらの端からも遠いところは出さない。 */
export function nearPair(x, y) {
  if (S.tool !== 'main' || !S.vd || x <= KEYS_W || y <= rollTop() || y >= rollBottom()) return null;
  const P = S.pitched;
  let best = null; let bd = Infinity;
  for (let i = 0; i + 1 < P.length; i++) {
    const a = P[i]; const b = P[i + 1];
    const xa = X(spanOf(a)[1]); const xb = X(spanOf(b)[0]);
    if (xb < KEYS_W - NEAR_PX || xa > W + NEAR_PX) continue;
    const dx = Math.min(Math.abs(x - xa), Math.abs(x - xb));
    if (dx >= NEAR_PX || dx >= bd) continue;
    const ba = boxOf(a); const bb = boxOf(b);
    if (y < Y(Math.max(ba.hi, bb.hi)) - 16 || y > Y(Math.min(ba.lo, bb.lo)) + 16) continue;
    bd = dx; best = pairKey(a, b);
  }
  return best;
}

/** 境目の記号（SVG）。 */
function connGlyphs() {
  const keys = connFocus();
  if (!keys.size) return '';
  const P = S.pitched;
  const trBy = new Map((S.vd.transitions || []).map((t) => [`${t.a}|${t.b}`, t]));
  const cc = planConnChanges();
  if (cc?.add) trBy.set(`${cc.add.a}|${cc.add.b}`, cc.add);
  const dr = S.drag;
  const pps = (W - KEYS_W) / S.view.span;
  let s = '';
  for (let i = 0; i + 1 < P.length; i++) {
    const a = P[i]; const b = P[i + 1];
    const key = pairKey(a, b);
    if (!keys.has(key)) continue;
    const xa = X(spanOf(a)[1]); const xb = X(spanOf(b)[0]);
    if (xb < KEYS_W - 40 || xa > W + 40) continue;
    const yA = Y(bandOf(a)); const yB = Y(bandOf(b));
    // 離した後の接続: 計画の切り離し（x ≠ 0）・吸着を重ねる
    let conn = !!a.connected_next;
    if (cc?.off.has(key)) conn = false;
    if (cc?.add && `${cc.add.a}|${cc.add.b}` === key) conn = true;
    if (!conn) {
      s += `<g data-conn="${key}" data-state="detached" pointer-events="none">`;
      for (const [x, y] of [[xa, yA], [xb, yB]]) {
        s += `<circle cx="${f1(x)}" cy="${f1(y)}" r="3.2" fill="#111113" stroke="${SEL}" stroke-width="1.3"/>`;
      }
      s += '</g>';
      continue;
    }
    // Alt の予告: 押している間（ドラッグしていない）と、Alt で掴んでまだ動かしていない間
    // （動かせば計画の切り離しが効いて、上の「切り離し」になる）
    const thisEdge = dr && dr.type === 'edge' && (dr.which === 'end' ? a.id : b.id) === dr.id;
    const cut = dr ? !!(thisEdge && dr.alt) : S.alt;
    const tr = trBy.get(key);
    let hl = 0; let hr = 0;
    if (tr) {
      const pv = S.trPreview && S.trPreview.keys.has(key) ? S.trPreview.value : null;
      [hl, hr] = pv == null ? [tr.hl, tr.hr] : trHalves(tr, pv);
    }
    const x0 = xa - hl * pps; const x1 = xb + hr * pps;
    let d = `M${f1(x0 - 8)} ${f1(yA)}L${f1(x0)} ${f1(yA)}`;
    for (let j = 1; j <= 24; j++) {
      const u = j / 24;
      d += `L${f1(x0 + (x1 - x0) * u)} ${f1(yA + (yB - yA) * (0.5 - 0.5 * Math.cos(Math.PI * u)))}`;
    }
    d += `L${f1(x1 + 8)} ${f1(yB)}`;
    const cx = (xa + xb) / 2; const cy = (yA + yB) / 2;
    s += `<g data-conn="${key}" data-state="${cut ? 'cut' : 'connected'}" pointer-events="none">`
      + `<path d="${d}" stroke="${SEL}" stroke-width="1.4" fill="none"${cut ? ' stroke-dasharray="3 3"' : ''}/>`
      + (cut
        ? `<circle cx="${f1(cx)}" cy="${f1(cy)}" r="3.2" fill="#111113" stroke="${SEL}" stroke-width="1.3"/>`
        : `<circle cx="${f1(cx)}" cy="${f1(cy)}" r="3.4" fill="${SEL}"/>`)
      + '</g>';
  }
  return s;
}

// ---------------------------------------------------------------- F0 の線
/** 曲線の path。edited = true は黄色の曲線（midis は `editedCurve()`。ドラッグ・スライダー・鉛筆の分を含む）。 */
function f0Path(times, midis, { edited = false, keep = null } = {}) {
  const gapSec = (S.vd?.f0?.hop_sec || 0.01) * 2.5;
  let d = ''; let pen = false; let prevT = -Infinity;
  for (let i = 0; i < midis.length; i++) {
    const m = midis[i];
    if (m == null) { pen = false; continue; }
    if (keep && !keep(times[i])) { pen = false; continue; }
    const t = edited ? warp(times[i]) : times[i];
    // 切り離して無音を挟んだところは線をつながない
    if (edited && t - prevT > gapSec) pen = false;
    prevT = t;
    const p = m;
    if (t < S.view.t0 - 0.1 || t > S.view.t0 + S.view.span + 0.1) { pen = false; continue; }
    d += (pen ? 'L' : 'M') + f1(X(t)) + ' ' + f1(Y(p));
    pen = true;
  }
  return d;
}

/** 黄色の曲線を**ノートごとの色**（ピッチの補正。corr.js）で描く: { 色: path の d }。
 * 色が変わるところは前の点から続ける（線は切れない）。 */
function f0PathsColored(times, midis, colorOf) {
  const gapSec = (S.vd?.f0?.hop_sec || 0.01) * 2.5;
  const out = new Map();
  let pen = false; let prevT = -Infinity; let last = null; let cur = null;
  const add = (c, str) => out.set(c, (out.get(c) || '') + str);
  for (let i = 0; i < midis.length; i++) {
    const m = midis[i];
    if (m == null) { pen = false; continue; }
    const t = warp(times[i]);
    if (t - prevT > gapSec) pen = false;
    prevT = t;
    if (t < S.view.t0 - 0.1 || t > S.view.t0 + S.view.span + 0.1) { pen = false; continue; }
    const p = `${f1(X(t))} ${f1(Y(m))}`;
    const c = colorOf(i);
    if (pen && c !== cur) add(c, `M${last}L${p}`);
    else add(c, `${pen ? 'L' : 'M'}${p}`);
    cur = c; last = p; pen = true;
  }
  return out;
}

// ---------------------------------------------------------------- ガイドとの対応（issue #53）
// 「ガイドに合わせる」を開いている間だけ（計画 = kind guide が載っている間）、計画の対象ノートについて:
// - 確かな対応（確かな発音の頭の組で裏付けられた組）は、テイクの頭とガイドの頭を細い中立の灰色線で結ぶ。
//   1 対多（組に複数のノート）は組の頭どうし・尻どうしの 2 本で**範囲**を示す（1 対 1 を装わない）
// - 確かな対応の無いノートは灰色の破線の丸
// - ホバーで「音程だけ対応／タイミングも対応（基準点・補間）／目標に届く」と、対応が無い理由
// 色は既存の灰色（WAS: 元の長さの線）だけ。ガイド = 暗いグレー、補正 = 黄→赤、手動 = 白の意味は変えない。
// 拡大率に応じて間引く（線は両端とも、印は中心が、前に描いたものに近すぎれば描かない）。
const CORR_MIN_PX = 6;
const CORR_MARK_R = 6;

function corrPlan() {
  const d = S.plan?.data;
  return d?.kind === 'guide' && S.showGuide && S.vd?.guide && Array.isArray(d.notes) ? d : null;
}

function corrLines() {
  const d = corrPlan();
  if (!d) return '';
  const gById = new Map((S.vd.guide_notes || []).map((g) => [g.id, g]));
  const segs = [];
  for (const pr of d.pairs || []) {
    if (!pr.confirmed) continue;
    const ts = pr.take.map((id) => S.byId.get(id)).filter((n) => n && n.kind === 'note');
    const gs = pr.guide.map((id) => gById.get(id)).filter((g) => g && g.pitch_midi != null);
    if (!ts.length || !gs.length) continue;
    const t0 = ts[0]; const t1 = ts[ts.length - 1]; const g0 = gs[0]; const g1 = gs[gs.length - 1];
    const key = `${t0.id}|${g0.id}`;
    segs.push({ key, x0: X(spanOf(t0)[0]), y0: Y(bandOf(t0)), x1: X(g0.start_sec), y1: Y(guideBandOf(g0)) });
    if (ts.length > 1 || gs.length > 1) {
      segs.push({ key, end: true, x0: X(spanOf(t1)[1]), y0: Y(bandOf(t1)), x1: X(g1.end_sec), y1: Y(guideBandOf(g1)) });
    }
  }
  segs.sort((a, b) => a.x0 - b.x0 || a.x1 - b.x1);
  let s = ''; let last = null;
  for (const g of segs) {
    if (Math.max(g.x0, g.x1) < KEYS_W || Math.min(g.x0, g.x1) > W) continue;
    // 両端とも前に描いた線から CORR_MIN_PX 以内なら間引く（組の尻と次の組の頭はテイク側が同じ点）
    if (last && g.x0 - last.x0 < CORR_MIN_PX && Math.abs(g.x1 - last.x1) < CORR_MIN_PX) continue;
    last = g;
    // 線と両端の小さな点（ほぼ縦になる線を、ガイドの線の縦の段差と見分けられるように）
    s += `<g data-corr="${g.key}" pointer-events="none">`
      + `<path data-corr-line="${g.key}"${g.end ? ' data-corr-end="1"' : ''} d="M${f1(g.x0)} ${f1(g.y0)}L${f1(g.x1)} ${f1(g.y1)}"`
      + ` stroke="${WAS}" stroke-width="1" stroke-opacity=".8" fill="none"/>`
      + `<circle cx="${f1(g.x0)}" cy="${f1(g.y0)}" r="1.8" fill="${WAS}"/>`
      + `<circle cx="${f1(g.x1)}" cy="${f1(g.y1)}" r="1.8" fill="${WAS}"/></g>`;
  }
  return s;
}

/** 文字列の見かけの幅（px。11 px の字: 全角 ≒ 11、半角 ≒ 6.2）。 */
function textW(t) {
  let w = 0;
  for (const ch of t) w += ch.charCodeAt(0) < 0x2000 ? 6.2 : 11;
  return w;
}

function corrMarks() {
  const d = corrPlan();
  if (!d) return '';
  let s = ''; let last = -Infinity;
  for (const r of d.notes) {
    if (r.confirmed) continue;
    const n = S.byId.get(r.note);
    if (!n) continue;
    const [a, b] = spanOf(n);
    const cx = X((a + b) / 2);
    if (cx < KEYS_W || cx > W || cx - last < 2 * CORR_MARK_R + 2) continue;
    last = cx;
    s += `<circle data-corr-mark="${r.note}" cx="${f1(cx)}" cy="${f1(Y(bandOf(n)))}" r="${CORR_MARK_R}"`
      + ` fill="none" stroke="${WAS}" stroke-width="1.2" stroke-dasharray="2.5 2" pointer-events="none"/>`;
  }
  return s;
}

/** ホバーしたノートの対応の説明（行の配列）。計画に無いノートは null。 */
export function corrText(id) {
  const d = corrPlan();
  const r = d?.notes.find((x) => x.note === id);
  if (!r) return null;
  const kind = r.timing === 'anchor' ? '基準点' : '補間';
  let head;
  if (r.pitch && r.timing) head = `タイミングも対応（${kind}）`;
  else if (r.pitch) head = '音程だけ対応';
  else if (r.timing) head = `タイミングだけ対応（${kind}）`;
  else head = '対応なし（直らない）';
  if (r.timing) head += r.reached ? '・目標に届く' : '・100% でも目標に届かない（追い越し・伸縮の上限）';
  const lines = [head];
  const pr = r.group != null ? d.pairs[r.group] : null;
  if (pr && r.guide.length) {
    const g = r.guide.length > 1 ? `${r.guide[0]}〜${r.guide[r.guide.length - 1]}` : r.guide[0];
    const t = pr.take.length > 1 ? `（1 対多: ${pr.take[0]}〜${pr.take[pr.take.length - 1]} の組）`
      : pr.guide.length > 1 ? '（1 対多）' : '';
    lines.push(`ガイド ${g}${t}${r.confirmed ? '' : ' は候補だけ'}`);
  }
  if (r.reason) lines.push(r.reason);
  if (r.timing_reason) lines.push(`タイミング: ${r.timing_reason}`);
  return lines;
}

function corrTip() {
  if (!corrPlan() || !S.noteHover || S.drag) return '';
  const lines = corrText(S.noteHover);
  const n = S.byId.get(S.noteHover);
  if (!lines || !n) return '';
  const [a, b] = spanOf(n);
  const w = Math.max(...lines.map(textW)) + 12;
  const h = lines.length * 15 + 6;
  const x = clamp(X((a + b) / 2) - w / 2, KEYS_W + 2, W - w - 2);
  const y = Math.max(rollTop() + 2, Y(bandOf(n)) - blobHMax() - 12 - h);
  let s = `<g data-corr-tip="${n.id}" pointer-events="none"><rect x="${f1(x)}" y="${f1(y)}" width="${f1(w)}" height="${h}" rx="2" fill="#232326"/>`;
  lines.forEach((l, i) => {
    s += `<text x="${f1(x + 6)}" y="${f1(y + 15 + i * 15)}" font-size="11" fill="${i ? '#bdbdc2' : SEL}">${esc(l)}</text>`;
  });
  return `${s}</g>`;
}

/** トラックビューで消している区間（クリップのミュート）を、斜線の帯で重ねる（ノートの無音とは別。トラックの頭が 0 の秒 =
 * ピアノロールの秒なのでそのまま描ける）。 */
function clipMuteBands(ROLL_T, ROLL_B) {
  const t = currentTrack();
  const ms = t?.mutes || [];
  if (!ms.length) return '';
  let s = '<defs><pattern id="clipMuteHatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
    + '<rect width="6" height="6" fill="#0d0d0f" fill-opacity=".55"/><line x1="0" y1="0" x2="0" y2="6" stroke="#3a3a3e" stroke-width="2"/></pattern></defs>';
  for (const [a, b] of ms) {
    const x0 = Math.max(KEYS_W, X(a)); const x1 = Math.min(W, X(b));
    if (x1 <= x0) continue;
    s += `<g data-clip-mute="${f1(a)}" pointer-events="none"><rect x="${f1(x0)}" y="${ROLL_T}" width="${f1(x1 - x0)}" height="${f1(ROLL_B - ROLL_T)}" fill="url(#clipMuteHatch)"/>`
      + `<line x1="${f1(X(a))}" y1="${ROLL_T}" x2="${f1(X(a))}" y2="${ROLL_B}" stroke="#5c5c62" stroke-dasharray="3 2"/>`
      + `<line x1="${f1(X(b))}" y1="${ROLL_T}" x2="${f1(X(b))}" y2="${ROLL_B}" stroke="#5c5c62" stroke-dasharray="3 2"/>`
      + (x1 - x0 > 90 ? `<text x="${f1(x0 + 6)}" y="${ROLL_T + 14}" font-size="10" fill="#8f8f94">クリップで消している</text>` : '')
      + '</g>';
  }
  return s;
}

// ---------------------------------------------------------------- 本体
export function render() {
  if (!svg) return;
  const r = svg.getBoundingClientRect();
  W = r.width || 1200;
  H = r.height || 396;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('class', `tool-${S.tool}`);
  if (!S.vd) { svg.innerHTML = ''; renderToolbar(); return; }

  const ROLL_T = rollTop(); const ROLL_B = rollBottom(); const ROLL_H = rollHeight();
  const dr = S.drag;
  const main = S.tool === 'main';
  const hovTool = main || S.tool === 'mute';       // ノートに乗ったら濃くする（鉛筆・はさみは出さない）
  let s = '';

  // ---- 鍵盤
  const selm = S.sel.length === 1 && S.byId.get(S.sel[0])?.pitch_midi != null
    ? Math.round(bandOf(S.byId.get(S.sel[0]))) : null;
  // 半音の行（見えているところだけ。上下の端の行は切って描く）
  const mTop = Math.floor(M(ROLL_T) + 0.5); const mBot = Math.ceil(M(ROLL_B) - 0.5);
  for (let m = mTop; m >= mBot; m--) {
    const ya = Math.max(ROLL_T, Y(m + 0.5)); const yb = Math.min(ROLL_B, Y(m - 0.5));
    if (yb - ya <= 0) continue;
    const bk = BLACK.indexOf(((m % 12) + 12) % 12) >= 0;
    s += `<rect x="${KEYS_W}" y="${f1(ya)}" width="${W - KEYS_W}" height="${f1(yb - ya)}" fill="${bk ? '#0d0d0f' : '#121215'}"/>`;
    const ka = Math.max(ROLL_T, Y(m + 0.5) + 0.5); const kb = Math.min(ROLL_B, Y(m - 0.5) - 0.5);
    if (kb - ka > 0) s += `<rect x="0" y="${f1(ka)}" width="${KEYS_W - 2}" height="${f1(kb - ka)}" fill="${bk ? '#2c2c30' : '#a6a6aa'}"/>`;
    const ty = Y(m) + 3.5;
    if ((m % 12 === 0 || (selm !== null && Math.abs(m - selm) <= 1)) && ty > ROLL_T + 9 && ty < ROLL_B) {
      s += `<text x="${KEYS_W - 6}" y="${f1(ty)}" text-anchor="end" font-size="10" fill="${bk ? '#a6a6aa' : '#151517'}" pointer-events="none">${noteName(m)}</text>`;
    }
  }

  // ---- 時間グリッド（タイムラインの秒 = S.off + ピアノロールの秒。小節・拍、テンポが無ければ秒。issue #18）
  const tk = ticks(S.view.t0 + S.off, S.view.t0 + S.view.span + S.off, pps(), GRID_MIN_PX);
  for (const g of tk.out) {
    const gx = Math.round(X(g.t - S.off)) + 0.5;
    if (gx < KEYS_W || gx > W) continue;
    s += `<line data-grid="${g.l}" x1="${gx}" y1="${ROLL_T}" x2="${gx}" y2="${ROLL_B}" stroke="${GRIDC[g.l]}" pointer-events="none"/>`;
  }

  // ---- ループ区間（S.loop はタイムラインの秒）
  if (S.loop) {
    const lx0 = X(S.loop[0] - S.off); const lx1 = X(S.loop[1] - S.off);
    s += `<rect x="${f1(lx0)}" y="${ROLL_T}" width="${f1(lx1 - lx0)}" height="${ROLL_B - ROLL_T}" fill="${SEL}" opacity=".07" pointer-events="none"/>`;
  }

  // ---- ガイド（濃いグレーの小さな波形＋線。テイクと同じ形式で、色だけ違う。issue #37: 線は 1.4 px で不透明、帯は薄く）
  // 「ガイドに合わせる」を開いている間だけ、**対応付けたガイドノートだけ通常の濃さ**で、
  // 他は薄くする（何がどこへ動くかを読めるように。要素は足さず濃さだけ変える）。
  const hmax = blobHMax();
  if (S.showGuide && S.vd.guide) {
    const focus = S.plan?.data?.kind === 'guide' ? S.plan.guides : null;
    const spans = [];
    for (const g of S.vd.guide_notes || []) {
      if (g.pitch_midi == null) continue;
      const on = !focus || focus.has(g.id);
      if (focus && on) spans.push([g.start_sec, g.end_sec]);
      const d = blobD(guideBlobPts(g, hmax));
      if (!d) continue;
      s += `<path data-guide="${g.id}" d="${d}" fill="${GUIDE}" opacity="${on ? '.55' : '.08'}" pointer-events="none"/>`;
    }
    const gs = S.vd.f0.guide_sec || []; const gm = S.vd.f0.guide_midi || [];
    if (focus) {
      const inside = (t) => spans.some(([a, b]) => t >= a && t <= b);
      const dim = f0Path(gs, gm, { keep: (t) => !inside(t) });
      const lit = f0Path(gs, gm, { keep: inside });
      if (dim) s += `<path data-guide-line="dim" d="${dim}" stroke="${GUIDE}" stroke-width="1.4" fill="none" opacity=".25" pointer-events="none"/>`;
      if (lit) s += `<path data-guide-line="1" d="${lit}" stroke="${GUIDE}" stroke-width="1.4" fill="none" pointer-events="none"/>`;
    } else {
      const gd = f0Path(gs, gm);
      if (gd) s += `<path data-guide-line="1" d="${gd}" stroke="${GUIDE}" stroke-width="1.4" fill="none" pointer-events="none"/>`;
    }
  }

  // ---- 音素境界。通常は下の音素レーンだけに置き、編集対象の境界だけ延長する。
  let tipStr = '';
  if (S.bounds.length) {
    const selectedSpans = S.sel.map((id) => S.byId.get(id)).filter(Boolean).map(spanOf);
    for (const b of S.bounds) {
      const x = X(boundSec(b));
      if (x < KEYS_W || x > W) continue;
      const t = boundSec(b);
      const active = S.drag?.type === 'bound' && S.drag.id === b.id || S.boundHover === b.id
        || selectedSpans.some(([start, end]) => t >= start - 0.002 && t <= end + 0.002);
      if (!S.showAllBounds && !active) continue;
      s += `<line data-bound-line="${b.id}" x1="${f1(x)}" y1="${ROLL_T}" x2="${f1(x)}" y2="${ROLL_B}" stroke="${SEL}"`
        + ` stroke-opacity="${active ? 0.55 : 0.16}" pointer-events="none"/>`;
    }
  }

  // ---- 音程の無い区間（息・囁き・無声の子音）: 同じ小さな波形を、ピッチ線なしで薄く
  // （ピッチ編集の対象外。前後のノートの高さに置く）。矢印のときは横につかめる（幅・タイミング。issue #35）
  const curve = editedCurve();
  const npc = noPitchCtx();
  const cxs = consonantXs();
  npEdgeXs = [];
  const ehNp = main && dr?.type === 'edge' && dr.nop ? { id: dr.id, which: dr.which }
    : main && !dr ? S.edgeHover : null;
  for (const n of S.notes) {
    if (n.kind === 'note') continue;
    const segs = takeBlobs(n, hmax, npc);
    if (!segs.length) continue;
    const d = blobsSplit(segs, cxs);
    const grabbed = dr?.nop && dr.id === n.id;
    const bc = bandColor(n);             // 帯の色 = タイミングの補正（issue #37）
    s += `<g class="npg${grabbed ? ' hov' : ''}${isSel(n.id) ? ' sel' : ''}${isMuted(n) ? ' muted' : ''}${hovTool && !dr && S.noteHover === n.id ? ' hov' : ''}">`;
    const opacity = bc === TAKE ? '.14' : '.45';
    if (d.main) s += `<path data-nopitch="${n.id}" d="${d.main}" fill="${bc}" fill-opacity="${opacity}" pointer-events="none"/>`;
    if (d.cons) s += `<path data-nopitch="${n.id}" data-cons="${n.id}" d="${d.cons}" fill="${bc === TAKE ? desat(bc) : bc}" fill-opacity="${opacity}" pointer-events="none"/>`;
    if (main || S.tool === 'cut' || S.tool === 'mute') s += noPitchHit(n, segs, noPitchConn(n));
    const [s0, s1] = spanOf(n);
    const first = segs[0][0]; const last = segs[segs.length - 1][segs[segs.length - 1].length - 1];
    if (isSel(n.id) && (Math.abs(n.start_sec - s0) > 0.0005 || Math.abs(n.end_sec - s1) > 0.0005)) {
      const oy = Math.min(...segs.flat().map((p) => p.y - p.h)) - 2;
      s += `<path data-orig="${n.id}" d="M${f1(X(n.start_sec))} ${f1(oy + 5)}V${f1(oy)}H${f1(X(n.end_sec))}V${f1(oy + 5)}" fill="none" stroke="${WAS}" stroke-width="1" pointer-events="none"/>`;
    }
    if (ehNp && ehNp.id === n.id) {
      // 端に乗っている・ドラッグ中: 明るい縦線（ノートの区切り = つかんでいる所）
      const cx = X(ehNp.which === 'start' ? s0 : s1);
      const p = ehNp.which === 'start' ? first : last;
      const hh = Math.max(8, p.h);
      s += `<line data-edge-hot="${ehNp.which}" x1="${f1(cx)}" y1="${f1(p.y - hh)}" x2="${f1(cx)}" y2="${f1(p.y + hh)}" stroke="${SEL}" stroke-width="2" pointer-events="none"/>`;
    }
    if (grabbed && dr.type === 'edge') {
      const cx = X(dr.which === 'start' ? s0 : s1);
      const p = dr.which === 'start' ? first : last;
      s += noPitchGlyph(n, dr.which, cx, p.y);
      if (dr.moved) tipStr += tip(cx, p.y - Math.max(8, p.h) - 14, `${sign(Math.round((S.plan?.x ?? dr.want ?? 0) * 1000))} ms`, 'middle');
    }
    if (grabbed && dr.type === 'note' && dr.axis === 'time' && dr.moved) {
      tipStr += tip((X(s0) + X(s1)) / 2, Math.min(first.y, last.y) - 22, `${sign(Math.round((S.plan?.x || 0) * 1000))} ms`, 'middle');
    }
    s += '</g>';
  }

  // ---- テイクの blob（小さな波形。中央＝ピッチ、端＝タイミング）
  // 当たり判定は今までどおり透明な矩形（中央＝ノート、両端＝端のつまみ）。見た目は波形だけ。
  const eh = main && dr?.type === 'edge' ? { id: dr.id, which: dr.which }
    : main && !dr ? S.edgeHover : null;
  let fadeSvg = '';            // フェードのつまみは全部のノートの上に重ねる（接した隣のノートの当たりに隠れない）
  for (let k = 0; k < S.pitched.length; k++) {
    const n = S.pitched[k];
    const b = boxOf(n);
    const x0 = X(b.s); const x1 = X(b.e);
    if (x1 < KEYS_W - 20 || x0 > W + 20) continue;
    // 縦は線の揺れの幅（10〜90 パーセンタイル）と、帯の中心 ±10 px の広い方
    const yc = Y(bandOf(n));
    // フェードのつまみ（帯の上の角。ノートでいちばん大きい音の帯の上の縁から 7 px 上）。ノートの当たりはそこまで広げる
    const yf = yc - noteHalfMax(n, hmax) - 7;
    const y0 = Math.min(Y(b.hi), yc - EDGE_Y, main ? yf - 5 : Infinity); const y1 = Math.max(Y(b.lo), yc + EDGE_Y);
    const sl = isSel(n.id);
    const dev = (n.deviation_cents ?? null) === null ? null
      : n.deviation_cents + (pitchOf(n) - (n.pitch_midi ?? 0)) * 100;
    // hov: フェードのつまみ（ノートの外に重ねて描く）に乗っている間もホバーの濃さのまま
    s += `<g class="nb${sl ? ' sel' : ''}${isMuted(n) ? ' muted' : ''}${hovTool && !dr && S.noteHover === n.id ? ' hov' : ''}">`;
    // 子音の区間は別の path（同じ .blob なので、選択・ホバーの濃さは同じ比でかかる）
    // 帯の色 = タイミングの補正（自動 = 黄 → 赤、手動 = 白。issue #37。corr.js）
    const bc = bandColor(n);
    const bd0 = blobsSplit(takeBlobs(n, hmax, npc), cxs);
    if (bd0.main) s += `<path class="blob" data-blob="${n.id}" d="${bd0.main}" fill="${bc}" pointer-events="none"/>`;
    // AI（Claude Code など）の編集が最後に当たっているノートは、帯の縁を AI の色に（state.js の aiNotesOf）
    if (bd0.main && S.aiNotes.has(n.id)) s += `<path data-ai="${n.id}" d="${bd0.main}" fill="none" stroke="${AI_EDGE}" stroke-width="1.2" stroke-opacity=".9" pointer-events="none"/>`;
    if (bd0.cons) s += `<path class="blob" data-cons="${n.id}" d="${bd0.cons}" fill="${desat(bc)}" pointer-events="none"/>`;
    s += `<rect data-note="${n.id}" x="${f1(x0)}" y="${f1(y0)}" width="${f1(Math.max(2, x1 - x0))}" height="${f1(Math.max(4, y1 - y0))}"`
      + ' fill="transparent" style="cursor:move"/>';
    // 元の長さ（issue #37）: **選択したノートだけ**、動かしていれば元の頭〜尻を帯のいちばん太い所の上 2 px に
    // 細いグレーの線と両端の短い縦線（寸法線の形）で。帯の端とのずれが、そのまま動かした量
    if (sl && (Math.abs(n.start_sec - b.s) > 0.0005 || Math.abs(n.end_sec - b.e) > 0.0005)) {
      const oy = yc - noteHalfMax(n, hmax) - 2;
      s += `<path data-orig="${n.id}" d="M${f1(X(n.start_sec))} ${f1(oy + 5)}V${f1(oy)}H${f1(X(n.end_sec))}V${f1(oy + 5)}" fill="none" stroke="${WAS}" stroke-width="1" pointer-events="none"/>`;
    }
    for (const which of ['start', 'end']) {
      // ノートの端（接続なら境目を共有して動く = col-resize、切り離しなら自分だけ = ew-resize）
      // 鉛筆・はさみのときは端をつままない（ツールの操作だけにする）
      if (!main) continue;
      const conn = which === 'start' ? n.connected_prev : n.connected_next;
      const [ha, hb] = edgeGrab(k, which, x0, x1, yc);
      s += `<rect data-note="${n.id}" data-edge="${which}" x="${f1(ha)}" y="${f1(yc - EDGE_Y)}" width="${f1(hb - ha)}" height="${2 * EDGE_Y}" fill="transparent" style="cursor:${conn && !S.alt ? 'col-resize' : 'ew-resize'}"/>`;
    }
    // フェードのつまみ（v3 §5）: ホバー中のノート・フェードをドラッグ中のノートの帯の上の両端に小さな四角
    // （DAW のクリップフェードと同じ位置と形）。フェードがあれば端からつまみまで細い線
    const fd = dr?.type === 'fade' && dr.id === n.id;
    if (main && (fd || (!dr && S.noteHover === n.id))) {
      const [fs0, fs1] = spanOf(n);
      const { fi, fo } = fadeOf(n);
      for (const [side, len] of [['in', fi], ['out', fo]]) {
        const ex = X(side === 'in' ? fs0 : fs1);
        const hx = X(side === 'in' ? fs0 + len : fs1 - len);
        const hot = (fd && dr.side === side) || (!dr && S.fadeHover === `${n.id}|${side}`);
        if (len > 0) fadeSvg += `<line x1="${f1(ex)}" y1="${f1(yf)}" x2="${f1(hx)}" y2="${f1(yf)}" stroke="#d6d6d6" stroke-opacity=".35" pointer-events="none"/>`;
        fadeSvg += `<rect data-note="${n.id}" data-fade="${side}" x="${f1(hx - 3.5)}" y="${f1(yf - 3.5)}" width="7" height="7" fill="${hot ? '#ffffff' : '#bdbdc2'}" style="cursor:ew-resize"/>`;
        if (fd && dr.side === side && dr.moved) tipStr += tip(hx, yf - 14, `${Math.round(len * 1000)} ms`, 'middle');
      }
    }
    // 端にポインタが乗っている・端をドラッグ中: 端に明るい縦線（つかめる所・動かしている所）
    if (eh && eh.id === n.id) {
      const cx = eh.which === 'start' ? x0 : x1;
      const hh = edgeHalf(n, eh.which, hmax);
      s += `<line data-edge-hot="${eh.which}" x1="${f1(cx)}" y1="${f1(yc - hh)}" x2="${f1(cx)}" y2="${f1(yc + hh)}" stroke="${SEL}" stroke-width="2" pointer-events="none"/>`;
    }
    // 音素境界のつまみ（blob の中の子音｜母音の境界。両端はノートの端のつまみが優先）
    for (const bd of S.bounds) {
      const t = boundSec(bd);
      if (t < b.s - 0.002 || t > b.e + 0.002) continue;
      const bx = X(t);
      if (bx < KEYS_W - EDGE || bx > W + EDGE) continue;
      const nearEdge = bx - x0 < EDGE || x1 - bx < EDGE;
      s += `<line x1="${f1(bx)}" y1="${f1(y0)}" x2="${f1(bx)}" y2="${f1(y1)}" stroke="${SEL}" stroke-opacity=".22" pointer-events="none"/>`;
      if (bd.movable === false || nearEdge || !main) continue;
      s += `<rect data-bound="${bd.id}" x="${f1(bx - EDGE / 2)}" y="${f1(y0)}" width="${EDGE}" height="${f1(Math.max(4, y1 - y0))}" fill="transparent" style="cursor:ew-resize"/>`;
    }
    if (dr && dr.moved && dr.type === 'note' && dr.axis === 'pitch' && dr.ids.includes(n.id)) {
      // 音程スナップ中は「G4 ±0¢」（帯の高さ = 平均の音程のいちばん近い半音とのずれ。v3 §4）
      const bm = bandOf(n); const r0 = Math.round(bm); const c0 = Math.round((bm - r0) * 100);
      const txt = pitchSnapOn() ? `${noteName(r0)} ${c0 === 0 ? '±0' : sign(c0)}¢`
        : dev !== null ? `${sign(Math.round(dev))}¢`
          : `${sign(Math.round((pitchOf(n) - (n.pitch_midi ?? 0)) * 100))}¢`;
      tipStr += tip(x1 + 6, (y0 + y1) / 2, txt, 'start');
    }
    if (dr && dr.moved && dr.type === 'edge' && dr.id === n.id) {
      const cx = dr.which === 'start' ? x0 : x1;
      tipStr += tip(cx, y0 - 14, `${sign(Math.round((S.plan?.x ?? dr.want ?? 0) * 1000))} ms`, 'middle');
    }
    if (dr && dr.moved && dr.type === 'note' && dr.axis === 'time' && dr.anchor?.id === n.id) {
      tipStr += tip((x0 + x1) / 2, y0 - 14, `${sign(Math.round((S.plan?.x || 0) * 1000))} ms`, 'middle');
    }
    s += '</g>';
  }

  s += fadeSvg;
  if (S.edgeDraft && S.edgeDraft.trackId === S.session?.current
    && !(S.edgeDraft.planId && S.plan?.data?.plan_id === S.edgeDraft.planId)) {
    const d = S.edgeDraft;
    const n = S.byId.get(d.id);
    if (n) {
      const [a, b] = spanOf(n);
      const x = X(d.which === 'start' ? a : b);
      s += `<line data-edge-draft="${d.id}" x1="${f1(x)}" y1="${ROLL_T}" x2="${f1(x)}" y2="${ROLL_B}" stroke="${SEL}" stroke-width="1.5" stroke-dasharray="4 4" pointer-events="none"/>`;
      tipStr += tip(x, ROLL_T + 18, '仮の端 · 計画待ち', 'middle');
    }
  }
  s += corrLines() + corrMarks();         // 帯の上・テイクの曲線の下

  // ---- はさみ: 接して並ぶノートの境目（ダブルクリックで結合）と、切る位置の線
  if (S.tool === 'cut') {
    for (let i = 0; i + 1 < S.pitched.length; i++) {
      const a = S.pitched[i]; const b = S.pitched[i + 1];
      if (Math.abs(b.start_sec - a.end_sec) > 0.006) continue;
      const ba = boxOf(a); const bb = boxOf(b);
      const jx = X(ba.e);
      if (jx < KEYS_W - EDGE || jx > W + EDGE) continue;
      const y0 = Y(Math.max(ba.hi, bb.hi)); const y1 = Y(Math.min(ba.lo, bb.lo));
      s += `<rect data-join="${a.id}|${b.id}" x="${f1(jx - EDGE / 2)}" y="${f1(y0)}" width="${EDGE}" height="${f1(Math.max(4, y1 - y0))}" fill="transparent"/>`;
    }
    for (let i = 0; i + 1 < S.notes.length; i++) {
      const a = S.notes[i]; const b = S.notes[i + 1];
      if (a.kind === 'note' || a.kind !== b.kind || Math.abs(b.start_sec - a.end_sec) > 0.006) continue;
      const jx = X(spanOf(a)[1]);
      if (jx < KEYS_W - EDGE || jx > W + EDGE) continue;
      const y = Y(noPitchLevel(a, npc).pa ?? noPitchLevel(a, npc).pb ?? npc.median);
      s += `<rect data-join="${a.id}|${b.id}" x="${f1(jx - EDGE / 2)}" y="${f1(y - EDGE_Y)}" width="${EDGE}" height="${2 * EDGE_Y}" fill="transparent"/>`;
    }
    const ch = S.cutHover;
    const hn = ch && S.byId.get(ch.id);
    if (hn && !S.drag) {
      const b = hn.kind === 'note' ? boxOf(hn) : null;
      const y = hn.kind === 'note' ? null : Y(noPitchLevel(hn, npc).pa ?? noPitchLevel(hn, npc).pb ?? npc.median);
      s += `<line x1="${f1(X(ch.t))}" y1="${f1(b ? Y(b.hi) : y - EDGE_Y)}" x2="${f1(X(ch.t))}" y2="${f1(b ? Y(b.lo) : y + EDGE_Y)}" stroke="${SEL}" stroke-width="1" pointer-events="none"/>`;
    }
  }

  // ---- blob に乗らない境界（無音・息の中）は、レーンのすぐ上の帯でつまむ
  if (S.bounds.length && main) {
    const band = Math.min(22, ROLL_H * 0.2);
    for (const bd of S.bounds) {
      if (bd.movable === false) continue;
      const t = boundSec(bd);
      const bx = X(t);
      if (bx < KEYS_W - EDGE || bx > W + EDGE) continue;
      if (S.pitched.some((n) => { const sp = spanOf(n); return t > sp[0] - 0.002 && t < sp[1] + 0.002; })) continue;
      s += `<rect data-bound="${bd.id}" x="${f1(bx - EDGE / 2)}" y="${f1(ROLL_B - band)}" width="${EDGE}" height="${f1(band)}" fill="transparent" style="cursor:ew-resize"/>`;
    }
  }

  // ---- 境界をドラッグ中のツールチップ（+7 ms）
  if (dr && dr.type === 'bound' && dr.moved) {
    const bd = S.boundById.get(dr.id);
    if (bd) {
      const bx = X(boundSec(bd));
      s += `<line x1="${f1(bx)}" y1="${ROLL_T}" x2="${f1(bx)}" y2="${f1(ROLL_B + LANE_H)}" stroke="${SEL}" stroke-width="1.5" pointer-events="none"/>`;
      tipStr += tip(bx, ROLL_T + 16, `${sign(Math.round((dr.dt || 0) * 1000))} ms`, 'middle');
    }
  }

  // ---- テイクの曲線（色 = ノートごとのピッチの補正。補正なしは黄。issue #37）
  for (const [c, d] of f0PathsColored(S.vd.f0.take_edited_sec, curve, lineColorer())) {
    s += `<path data-line="${c}" d="${d}" stroke="${c}" stroke-width="1.6" fill="none" pointer-events="none"/>`;
  }

  s += connGlyphs();
  s += clipMuteBands(ROLL_T, ROLL_B);

  if (dr && dr.type === 'box' && dr.moved) {
    s += `<rect x="${f1(Math.min(dr.x0, dr.x1))}" y="${f1(Math.min(dr.y0, dr.y1))}" width="${f1(Math.abs(dr.x1 - dr.x0))}" height="${f1(Math.abs(dr.y1 - dr.y0))}" fill="${SEL}" fill-opacity=".06" stroke="${SEL}" stroke-opacity=".6" pointer-events="none"/>`;
  }
  // 鍵盤の上はどのツールでも矢印（鉛筆・はさみのカーソルにしない。issue #6）。帯・線の後に重ねる
  s += `<rect data-keys="1" x="0" y="${ROLL_T}" width="${KEYS_W}" height="${f1(ROLL_B - ROLL_T)}" fill="transparent" style="cursor:default"/>`;
  // タイムスケールは帯・線の後に描く（縦に拡大・スクロールして上にはみ出した帯を隠す）
  s += scaleSvg();
  s += tipStr + corrTip();

  // ---- 歌詞・音素レーン（かな 1 段＋音素 1 段。子音は暗く、母音は明るく）
  s += `<rect data-lane="1" x="0" y="${ROLL_B}" width="${W}" height="${LANE_H}" fill="#121214" style="cursor:text"/>`
    + `<line x1="0" y1="${ROLL_B + 0.5}" x2="${W}" y2="${ROLL_B + 0.5}" stroke="#2c2c30"/>`
    + `<line x1="${KEYS_W - 0.5}" y1="0" x2="${KEYS_W - 0.5}" y2="${H}" stroke="#2c2c30"/>`;
  for (const bd of S.vd.boundaries || []) {
    const x = X(warp(bd.edited_sec));
    if (x < KEYS_W || x > W) continue;
    s += `<line x1="${f1(x)}" y1="${ROLL_B + 4}" x2="${f1(x)}" y2="${ROLL_B + LANE_H - 4}" stroke="#2c2c30" pointer-events="none"/>`;
  }
  // 聞き取りの候補（issue #54）がある区間は、今の歌詞の代わりに候補を出す（下の asrLane）
  const cand = asrCandidate();
  const underCand = (a, b) => !!cand && a < cand.end_sec && b > cand.start_sec;
  if (S.ph) {
    // 上段: かな（音節ごと）
    for (const sy of S.ph.syllables || []) {
      if (underCand(sy.start_sec, sy.end_sec)) continue;
      const mx = (X(warp(sy.edited_start_sec)) + X(warp(sy.edited_end_sec, 'left'))) / 2;
      if (mx < KEYS_W || mx > W) continue;
      const sl = S.pitched.some((n) => isSel(n.id)
        && sy.edited_end_sec > n.edited_start_sec && sy.edited_start_sec < n.edited_end_sec);
      const ent = S.lyricsEntries.find((e) => e.start_sec == null
        || (e.start_sec < sy.end_sec && e.end_sec > sy.start_sec));
      const local = ent ? (S.ph.syllables || []).filter((x) => x.index <= sy.index
        && (ent.start_sec == null || (ent.start_sec < x.end_sec && ent.end_sec > x.start_sec))).length - 1 : -1;
      const estimated = ent?.origin === 'estimated'
        && !ent.confirmed_syllables?.includes(local);
      s += `<text data-kana="${sy.index}" x="${f1(mx)}" y="${ROLL_B + 19}" text-anchor="middle" font-size="13" fill="${sl && !estimated ? SEL : (estimated ? '#85858a' : '#d6d6d6')}" pointer-events="none">${esc(sy.kana)}</text>`;
    }
    // 下段: 音素（子音 #5c5c62 / 母音・息 #9a9a9e。無音は出さない）
    for (const p of S.ph.phonemes || []) {
      if (p.label === 'silence') continue;
      const [a, b2] = phSpan(p);
      const mx = (X(a) + X(b2)) / 2;
      if (mx < KEYS_W || mx > W) continue;
      if (X(b2) - X(a) < 7) continue;          // 狭すぎるところは文字を出さない
      const col = p.label === 'consonant' ? '#5c5c62'
        : '#9a9a9e';
      s += `<text data-ph="${p.id}" x="${f1(mx)}" y="${ROLL_B + 37}" text-anchor="middle" font-size="11" fill="${col}" pointer-events="none">${esc(p.text)}</text>`;
    }
  } else {
    // 聞き取りの枠（issue #54）と重なるので、その間は案内を出さない
    if (!cand && !asrRunning()) s += `<text x="${KEYS_W + 8}" y="${ROLL_B + 27}" font-size="11" fill="#5c5c62" pointer-events="none">ここをダブルクリックして歌詞を入れると音素が出ます</text>`;
    for (const n of S.pitched) {
      const [a, b2] = spanOf(n);
      const mx = (X(a) + X(b2)) / 2;
      if (mx < KEYS_W || mx > W || !n.text || underCand(n.start_sec, n.end_sec)) continue;
      s += `<text x="${f1(mx)}" y="${ROLL_B + 19}" text-anchor="middle" font-size="13" fill="${isSel(n.id) ? SEL : '#d6d6d6'}" pointer-events="none">${esc(n.text)}</text>`;
    }
  }
  s += asrLane(ROLL_B, cand);
  if (dr && dr.type === 'lane' && dr.moved) {
    const lx = Math.min(dr.x0, dr.x1); const lw = Math.abs(dr.x1 - dr.x0);
    s += `<rect x="${f1(lx)}" y="${ROLL_T}" width="${f1(lw)}" height="${H - ROLL_T}" fill="${SEL}" fill-opacity=".06" pointer-events="none"/>`
      + `<rect x="${f1(lx)}" y="${ROLL_B}" width="${f1(lw)}" height="${LANE_H}" fill="none" stroke="${SEL}" stroke-opacity=".6" pointer-events="none"/>`;
  }

  s += `<g id="ph" transform="translate(${f1(X(araEditorHead() - S.off))},0)" pointer-events="none">`
    + `<line x1="0" y1="0" x2="0" y2="${H}" stroke="${SEL}" stroke-width="1"/>`
    + `<path d="M-4,0 L4,0 L0,6 Z" fill="${SEL}"/></g>`;

  svg.innerHTML = s;
  renderToolbar();
  for (const fn of hooks.render) fn();
}

/** 聞き取り（issue #54）: 聞き取っている区間の枠と、候補の文字（歌詞レーンの上段に薄く。語は聞こえた時刻に置く）。 */
function asrLane(ROLL_B, cand) {
  let s = '';
  const box = (r, kind) => {
    const x0 = Math.max(KEYS_W, X(toEdited(r.start_sec)));
    const x1 = Math.min(W, X(toEdited(r.end_sec)));
    if (x1 - x0 < 2) return null;
    s += `<rect data-asr="${kind}" x="${f1(x0)}" y="${ROLL_B + 3}" width="${f1(x1 - x0)}" height="21" rx="2" fill="#18181b" stroke="#5c5c62" stroke-dasharray="3 3" pointer-events="none"/>`;
    return [x0, x1];
  };
  const run = asrRunning();
  if (run) {
    const b = box(run, 'running');
    if (b && b[1] - b[0] > 90) s += `<text x="${f1(b[0] + 6)}" y="${ROLL_B + 18}" font-size="11" fill="#5c5c62" pointer-events="none">聞き取っている…</text>`;
  }
  if (!cand) return s;
  const b = box(cand, 'candidate');
  if (!b) return s;
  const [x0, x1] = b;
  const words = cand.words?.length ? cand.words
    : [{ text: cand.text || '（聞き取れない）', start_sec: cand.start_sec }];
  const width = (t) => [...t].reduce((a, c) => a + (c.charCodeAt(0) < 0x2e80 ? 7 : 13), 0);
  s += `<clipPath id="asrClip"><rect x="${f1(x0)}" y="${ROLL_B}" width="${f1(x1 - x0)}" height="${LANE_H}"/></clipPath>`
    + `<g data-asr-text="1" clip-path="url(#asrClip)" pointer-events="none">`;
  let cursor = -Infinity;
  for (const w of words) {
    const x = Math.max(X(toEdited(w.start_sec)), cursor + 2, x0 + 4);
    const low = w.probability != null && w.probability < 0.5;
    s += `<text data-asr-word="1" x="${f1(x)}" y="${ROLL_B + 19}" font-size="13" fill="#85858a"${low ? ' text-decoration="underline"' : ''}>${esc(w.text)}</text>`;
    cursor = x + width(w.text);
  }
  return `${s}</g>`;
}

/** タイムスケール（目盛りはタイムラインの秒 = トラックの位置 S.off ＋ ピアノロールの秒）とループの印。 */
function scaleSvg() {
  let s = `<rect x="0" y="0" width="${W}" height="${SCALE_H}" fill="#111113"/>`;
  if (S.loop) {
    const lx0 = X(S.loop[0] - S.off); const lx1 = X(S.loop[1] - S.off);
    s += `<rect x="${f1(lx0)}" y="0" width="${f1(lx1 - lx0)}" height="${SCALE_H}" fill="${SEL}" opacity=".07" pointer-events="none"/>`
      + `<rect x="${f1(lx0)}" y="${SCALE_H - 3}" width="${f1(lx1 - lx0)}" height="3" fill="${SEL}" opacity=".5" pointer-events="none"/>`;
  }
  // 目盛りはグリッドと同じ（小節・拍か秒）。ラベルは 50 px 以上あける
  const tk = ticks(S.view.t0 + S.off, S.view.t0 + S.view.span + S.off, pps(), GRID_MIN_PX, 50);
  for (const g of tk.out) {
    if (g.l === 2 && !g.lab) continue;
    const tx = X(g.t - S.off);
    if (tx < KEYS_W - 1) continue;
    s += `<line x1="${f1(tx)}" y1="${SCALE_H - (g.lab || g.l === 0 ? 8 : 4)}" x2="${f1(tx)}" y2="${SCALE_H}" stroke="#3a3a3e" pointer-events="none"/>`;
    if (g.lab) s += `<text data-rlab="1" x="${f1(tx + 3)}" y="12" font-size="10" fill="#8f8f94" pointer-events="none">${g.lab}</text>`;
  }
  s += `<rect data-scale="1" x="${KEYS_W}" y="0" width="${W - KEYS_W}" height="${SCALE_H}" fill="transparent" style="cursor:default"/>`;
  return s;
}

export function movePlayhead() {
  const g = svg?.querySelector('#ph');
  if (g) g.setAttribute('transform', `translate(${f1(X(araEditorHead() - S.off))},0)`);
  const c = document.querySelector('#clock');
  if (c) c.textContent = fmtTime(S.head);
  for (const fn of hooks.head) fn();
}

/** 再生中、再生位置がピアノロールの表示範囲を出たら画面送りする（モックと同じ。上の白枠も追従する）。
 * ヘッダーの「再生位置に追従」（F。issue #40）がオフなら送らない。 */
export function follow() {
  if (!S.vd || S.drag || !G.follow) return;
  const t = araEditorHead() - S.off;
  const total = totalSec();
  if (t < 0 || t > total) return;                   // 編集中のトラックの外（他のトラックだけ鳴っている）
  const v = S.view;
  if (t >= v.t0 && t <= v.t0 + v.span) return;
  v.t0 = clamp(t - v.span * 0.02, 0, Math.max(0, total - v.span));
  render();
}

export function renderToolbar() {
  const q = (id) => document.querySelector(id);
  q('#icPlay').hidden = S.playing;
  q('#icStop').hidden = !S.playing;
  q('#bGuide').setAttribute('aria-pressed', S.showGuide ? 'true' : 'false');
  q('#bGuide').disabled = !guideShown();
  q('#bMacro').disabled = !guideShown();
  // 取り消し（issue #16）: まだ当たっていない操作があればそれ、無ければエンジンの履歴の最後の操作。
  // 確定中でも押せる（順番待ちに入る）。ツールチップとメニューに「元に戻す: ○○」（AI の操作は「AI · ○○」）
  const { u, rd } = undoLabels();
  const bu = q('#bUndo'); const br = q('#bRedo');
  bu.disabled = !u;
  br.disabled = !rd;
  // ツールチップのキーはコマンドの表と設定から（issue #22。設定で変えればここも変わる）
  const tip = (b, t) => { if (b && b.title !== t) { b.title = t; b.setAttribute('aria-label', t); } };
  tip(bu, withKey(u ? `元に戻す: ${u}` : '元に戻す', 'undo'));
  tip(br, withKey(rd ? `やり直す: ${rd}` : 'やり直す', 'redo'));
  const historyLabel = q('#undoLabel');
  if (historyLabel) historyLabel.textContent = u ? `元に戻す: ${u}` : '元に戻す: なし';
  const strokeActions = q('#strokeActions');
  if (strokeActions) {
    const phase = S.strokePhase;
    strokeActions.hidden = !['failed', 'checking'].includes(phase);
    const msg = q('#strokeMessage');
    if (msg) msg.textContent = phase === 'checking' ? '適用状態を確認中 · 描線を保持' : '描線を確定できませんでした';
    q('#strokeRetry').disabled = phase === 'checking';
  }
  tip(q('#bPlay'), withKey('再生／停止', 'play'));
  tip(q('#bToolMain'), withKey('メインツール', 'tool-main'));
  tip(q('#bToolDraw'), `${withKey('鉛筆', 'tool-draw')}: ピッチを描く`);
  tip(q('#bToolCut'), `${withKey('はさみ', 'tool-cut')}: ノートを分ける（境目をダブルクリックで結合）`);
  tip(q('#bToolMute'), `${withKey('ミュート', 'tool-mute')}: クリックで無音にする／戻す（なぞるとまとめて）`);
  tip(q('#bGuide'), withKey('ガイドを重ねて表示', 'guide-view'));
  tip(q('#bMacro'), withKey('ガイドに合わせる…', 'guide-match'));
  // スナップ（issue #18）: 押している間は濃く。細かさのプルダウンは表示（小節・拍 / 分:秒）に合わせて中身を替える
  q('#bSnapT').setAttribute('aria-pressed', G.snapT ? 'true' : 'false');
  q('#bSnapP').setAttribute('aria-pressed', G.snapP ? 'true' : 'false');
  tip(q('#bSnapT'), `${withKey('時間スナップ', 'snap-time')}。ドラッグ中 Shift で解除`);
  q('#bFollow').setAttribute('aria-pressed', G.follow ? 'true' : 'false');
  tip(q('#bFollow'), withKey('再生位置に追従', 'follow'));
  tip(q('#bSnapP'), `${withKey('音程スナップ: 平均の音程を半音に', 'snap-pitch')}。ドラッグ中 Shift で解除`);
  const sel = q('#gridDiv');
  const mode = barsMode() ? 'bars' : 'sec';
  if (sel && sel.dataset.mode !== mode) {
    sel.dataset.mode = mode;
    sel.innerHTML = (mode === 'bars' ? GRID_BARS : GRID_SEC)
      .map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
  }
  if (sel && sel.value !== currentDiv()) sel.value = currentDiv();
  renderTempo();
  // メニューバー（Electron）: 「元に戻す: ○○」とキーの表記（変わったときだけ main に送る）
  syncAppMenu();
  q('#clock').textContent = fmtTime(S.head);
  for (const [id, tool] of [['#bToolMain', 'main'], ['#bToolDraw', 'draw'], ['#bToolCut', 'cut'], ['#bToolMute', 'mute']]) {
    const b = q(id);
    if (b) b.setAttribute('aria-pressed', S.tool === tool ? 'true' : 'false');
  }
}

/** 横ズーム（Ctrl+ホイール）。縦は固定。 */
export function zoom(factor, anchorX) {
  const total = totalSec();
  const tAnchor = T(clamp(anchorX, KEYS_W, W));
  const span = clamp(S.view.span * factor, 0.15, total);
  S.view.t0 = clamp(tAnchor - (tAnchor - S.view.t0) * (span / S.view.span), 0, Math.max(0, total - span));
  S.view.span = span;
}

/** 範囲（編集後の秒）が画面に収まるように寄せる。発声区間へのジャンプに使う。 */
export function focusRange(a, b, margin = 0.35) {
  const total = totalSec();
  const want = clamp((b - a) + margin * 2, 0.15, total);
  S.view.span = want;
  S.view.t0 = clamp((a + b) / 2 - want / 2, 0, Math.max(0, total - want));
}

/** 縦ズーム（既定 Ctrl+ホイール）: ポインタの下の音程を中心に、見えている半音の数を 6〜36 に。 */
export function zoomPitch(factor, anchorY) {
  const p = pv();
  const m = M(clamp(anchorY, rollTop(), rollBottom()));
  const k = (p.top - m) / p.span;                 // ポインタの音程が上から何割の所か（そのまま保つ）
  const span = clamp(p.span * factor, PITCH_VIEW.MIN, PITCH_VIEW.MAX);
  S.pv = clampPitchView({ top: m + k * span, span });
}

/** 縦スクロール（既定 ホイール）: dm 半音だけ上へ（負なら下へ）。 */
export function scrollPitch(dm) {
  const p = pv();
  S.pv = clampPitchView({ top: p.top + dm, span: p.span });
}

export function pan(dx) {
  const total = totalSec();
  S.view.t0 = clamp(S.view.t0 + dx / (W - KEYS_W) * S.view.span, 0,
    Math.max(0, total - S.view.span));
}
