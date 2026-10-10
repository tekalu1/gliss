// 時間グリッド・スナップ・テンポ（issue #18。proposal/v3.html §4・§8）。
//
// - テンポはセッション（エンジンの session.json の `tempo`）。伴奏などの WAV の iXML（PreSonus のテンポマップ）から
//   エンジンが自動で読み、無ければヘッダーの「120 BPM 4/4」で手で入れる。どちらも無いうちは秒のグリッド。
// - 表示（ルーラーの読み方）: テンポがあれば 小節・拍、無ければ 分:秒（ルーラーの右クリックで切り替え）。
// - グリッドの細かさ（ヘッダーのスナップのアイコンの隣のプルダウン。Studio One のクオンタイズ値と同じ位置付け）:
//   小節・拍のとき 自動／1/1〜1/32／3 連符、分:秒のとき 自動／秒の刻み。**トラックビューとエディターで共通**
//   （プルダウンが 1 つ。自動のときは、それぞれのズームで線の間隔が詰まりすぎない刻みになるので、上は粗く下は細かい）。
// - スナップ: 時間（N）と音程・半音（Shift+N）。既定はどちらもオフ。Shift を押している間は解除（Studio One と同じ）。
//   時間スナップはノートの端・移動・はさみ・トラックの位置ずらしに効く。
import { S } from './state.js';

export const GRID_BARS = [
  ['auto', '自動'], ['1/1', '1/1'], ['1/2', '1/2'], ['1/4', '1/4'], ['1/8', '1/8'], ['1/16', '1/16'], ['1/32', '1/32'],
  ['1/2T', '1/2 3連'], ['1/4T', '1/4 3連'], ['1/8T', '1/8 3連'], ['1/16T', '1/16 3連'],
];
export const GRID_SEC = [
  ['auto', '自動'], ['1', '1 秒'], ['0.5', '0.5 秒'], ['0.25', '0.25 秒'], ['0.1', '0.1 秒'], ['0.05', '0.05 秒'],
  ['0.01', '0.01 秒'],
];
// 画面の設定（ユーザー設定に残す。取り消しの履歴には入れない = DAW と同じ）
export const G = {
  snapT: false,           // 時間スナップ（N）
  snapP: false,           // 音程スナップ（Shift+N）
  divBars: 'auto',        // 小節・拍のときの細かさ
  divSec: 'auto',         // 分:秒のときの細かさ
  fmt: null,              // ルーラーの読み方: null = テンポがあれば小節・拍、'sec' = 分:秒に固定、'bars' = 小節・拍
  shift: false,           // Shift を押している（スナップを解除する）
  // 再生位置に追従する（オートスクロール。F。issue #40）。再生中に再生位置が表示範囲を出たら画面を送る
  // （エディターとトラックビューのそれぞれ）。既定はオン（前の版と同じ動き）
  follow: true,
};
export const DEFAULT_TEMPO = { bpm: 120, num: 4, den: 4, start_sec: 0 };

/** 設定（スナップ・細かさ・再生位置に追従）を読む（起動時。main の bootstrap の grid）。 */
export function loadGrid(g) {
  if (!g || typeof g !== 'object') return;
  G.snapT = !!g.snapT;
  G.snapP = !!g.snapP;
  if (GRID_BARS.some(([v]) => v === g.divBars)) G.divBars = g.divBars;
  if (GRID_SEC.some(([v]) => v === g.divSec)) G.divSec = g.divSec;
  G.follow = g.follow !== false;
}
/** 設定をユーザー設定に残す（取り消しの履歴には入れない）。 */
export function saveGrid() {
  window.api?.saveState?.({
    grid: { snapT: G.snapT, snapP: G.snapP, divBars: G.divBars, divSec: G.divSec, follow: G.follow },
  });
}

/** いまのテンポ（ヘッダーのドラッグ中はその見かけの値）。無ければ null。 */
export function tempo() {
  return S.tempoPreview || S.session?.tempo || null;
}
/** ルーラーとグリッドが小節・拍か。 */
export function barsMode() {
  return !!tempo() && G.fmt !== 'sec';
}
/** 4 分音符・拍（分母の音符）・小節の秒。 */
export function beats(t = tempo()) {
  const q = 60 / t.bpm;
  const beat = q * 4 / t.den;
  return { q, beat, bar: beat * t.num };
}
/** 細かさの値 → 秒（'auto' は null）。 */
export function divSec(v, t = tempo()) {
  if (!v || v === 'auto') return null;
  if (!t || !barsMode()) return +v;
  const m = /^1\/(\d+)(T?)$/.exec(v);
  if (!m) return null;
  const q = 60 / t.bpm;
  return q * 4 / +m[1] * (m[2] ? 2 / 3 : 1);
}
export function currentDiv() { return barsMode() ? G.divBars : G.divSec; }
export function divLabel() {
  const v = currentDiv();
  const list = barsMode() ? GRID_BARS : GRID_SEC;
  return (list.find(([k]) => k === v) || list[0])[1];
}

const SEC_STEPS = [0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
const E = 1e-6;

/** 線を引く刻み（秒）: 細かさの値（自動なら最小）以上で、間隔が minPx 以上になるもの。 */
function drawStep(pxPerSec, minPx) {
  const want = divSec(currentDiv());
  if (barsMode()) {
    const { beat, bar } = beats();
    const cands = [beat / 8, beat / 4, beat / 2, beat, bar];
    for (let k = 2; k <= 256; k *= 2) cands.push(bar * k);
    if (want && !cands.some((c) => Math.abs(c - want) < 1e-9)) cands.push(want);     // 3 連符
    cands.sort((a, b) => a - b);
    for (const c of cands) {
      if (want && c < want - 1e-9) continue;
      if (c * pxPerSec >= minPx) return c;
    }
    return cands[cands.length - 1];
  }
  for (const c of SEC_STEPS) {
    if (want && c < want - 1e-9) continue;
    if (c * pxPerSec >= minPx) return c;
  }
  return SEC_STEPS[SEC_STEPS.length - 1];
}

/** スナップの刻み（秒）: 細かさを選んでいればそれ、自動なら線の刻み（そのビューのズームで）。 */
export function snapStep(pxPerSec, minPx) {
  return divSec(currentDiv()) || drawStep(pxPerSec, minPx);
}

/** グリッドの原点（1 小節目の頭。分:秒なら 0）。 */
function origin() { return barsMode() ? tempo().start_sec : 0; }

/** t（タイムラインの秒）をグリッドに寄せる。 */
export function snapTime(t, step) {
  const o = origin();
  return o + Math.round((t - o) / step) * step;
}

/** 時間スナップが効いているか（オンで、Shift を押していない）。 */
export function timeSnapOn(e) { return G.snapT && !(e ? e.shiftKey : G.shift); }
export function pitchSnapOn(e) { return G.snapP && !(e ? e.shiftKey : G.shift); }
/** 鉛筆が半音に沿うか: 音程スナップの設定を、Shift を押している間だけ反転する（2026-10-10 承認）。 */
export function penSnapOn(e) { return G.snapP !== !!(e ? e.shiftKey : G.shift); }

function fmtSec(t, step) {
  const at = Math.abs(t) < 1e-9 ? 0 : Math.abs(t);
  const m = Math.floor(at / 60 + 1e-9); const s = at - m * 60;
  const d = step < 0.1 - 1e-9 ? 2 : step < 1 - 1e-9 ? 1 : 0;
  let ss = d ? s.toFixed(d) : String(Math.round(s));
  if (s < 10 - 1e-9) ss = `0${ss}`;
  return `${t < -1e-9 ? '−' : ''}${m}:${ss}`;
}

/** 小節・拍の読み（1 小節目 = 1）。 */
export function barBeat(t) {
  const tp = tempo();
  const { beat, bar } = beats(tp);
  const x = (t - tp.start_sec) / bar;
  const b = Math.floor(x + E);
  const be = Math.floor((t - tp.start_sec - b * bar) / beat + E);
  return { bar: b + 1, beat: be + 1 };
}

/** [a, b]（タイムラインの秒）の目盛り [{t, l: 0 小節・1 拍・2 それより細かい, lab}] と線の刻み。
 * minPx: 線の間隔の下限、labPx: ラベルの間隔の下限。 */
export function ticks(a, b, pxPerSec, minPx = 22, labPx = 50) {
  const step = drawStep(pxPerSec, minPx);
  const out = [];
  if (barsMode()) {
    const tp = tempo();
    const { beat, bar } = beats(tp);
    const st = tp.start_sec;
    // 小節の番号は間隔が labPx 以上になるように間引く（1・2・4・8… 小節ごと）
    let every = 1;
    while (bar * every * pxPerSec < labPx && every < 4096) every *= 2;
    const i0 = Math.ceil((a - st) / step - E); const i1 = Math.floor((b - st) / step + E);
    if (i1 - i0 > 5000) return { out, step };
    for (let i = i0; i <= i1; i++) {
      const t = st + i * step;
      const bi = (t - st) / bar; const be = (t - st) / beat;
      const isBar = Math.abs(bi - Math.round(bi)) < 1e-6;
      const isBeat = Math.abs(be - Math.round(be)) < 1e-6;
      const n = Math.round(bi);
      let lab = '';
      if (isBar && ((n % every) + every) % every === 0) lab = String(n + 1);
      else if (isBeat && !isBar && beat * pxPerSec >= 70) {
        const bb = barBeat(t);
        lab = `${bb.bar}.${bb.beat}`;
      }
      out.push({ t, l: isBar ? 0 : isBeat ? 1 : 2, lab });
    }
    return { out, step };
  }
  // 分:秒: 線は step ごと、ラベルは labPx 以上あく刻みごと
  let labStep = step;
  for (const c of SEC_STEPS) { if (c >= step - 1e-9 && c * pxPerSec >= labPx) { labStep = c; break; } }
  if (labStep * pxPerSec < labPx) labStep = SEC_STEPS[SEC_STEPS.length - 1];
  const big = labStep >= 1 ? 1 : labStep;
  const i0 = Math.ceil(a / step - E); const i1 = Math.floor(b / step + E);
  if (i1 - i0 > 5000) return { out, step };
  for (let i = i0; i <= i1; i++) {
    const t = i * step;
    const isLab = Math.abs(t / labStep - Math.round(t / labStep)) < 1e-6;
    const isBig = Math.abs(t / big - Math.round(t / big)) < 1e-6;
    out.push({ t, l: isLab ? 0 : isBig ? 1 : 2, lab: isLab ? fmtSec(t, labStep) : '' });
  }
  return { out, step };
}

/** ヘッダーの表示「120 BPM」の数字（0.01 刻み。整数なら整数）。 */
export function bpmText(t = tempo()) {
  if (!t) return '—';
  return String(Math.round(t.bpm * 100) / 100);
}
export function sigText(t = tempo()) {
  return t ? `${t.num}/${t.den}` : '4/4';
}
