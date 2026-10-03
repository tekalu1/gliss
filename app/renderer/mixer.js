// トラックの音量・パンの目盛りと表記（見出しのスライダー・ノブ。docs/track-view.md の「見出しの音量・パン」）。
// 画面の部品（tracks.js）・再生（audio.js）・テストが同じ式を使う。DOM には触れない。
//
//  - 音量は dB。−∞〜+6 dB。エンジンの `gain_db` は −60〜+6 で、**−60 は無音（−∞）**。
//  - スライダーの位置 p（0〜1）は 3 乗に近い目盛り: 線形の増幅率が (p / 0.8)^N（N は右端がちょうど +6 dB になる値）。
//    0 dB が 80% の所、左端が −∞。
//  - パンは −1（左いっぱい）〜+1（右いっぱい）。表示は L100〜C〜R100。

export const GAIN_MIN_DB = -60;
export const GAIN_MAX_DB = 6;
export const ZERO_POS = 0.8;                // 0 dB のスライダー上の位置
const N = GAIN_MAX_DB / (20 * Math.log10(1 / ZERO_POS));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

/** スライダーの位置（0〜1）→ dB（−60 以下は無音 = GAIN_MIN_DB）。 */
export function posToDb(p) {
  if (!(p > 0)) return GAIN_MIN_DB;
  return clamp(Math.round(20 * N * Math.log10(p / ZERO_POS) * 1000) / 1000, GAIN_MIN_DB, GAIN_MAX_DB);
}

/** dB → スライダーの位置（無音は 0）。 */
export function dbToPos(db) {
  if (!(db > GAIN_MIN_DB)) return 0;
  return clamp(ZERO_POS * Math.pow(10, db / (20 * N)), 0, 1);
}

/** dB → 線形の増幅率（無音は 0）。 */
export function dbToGain(db) {
  return db > GAIN_MIN_DB ? Math.pow(10, db / 20) : 0;
}

/** 数字だけの表記（単位は付けない）。0 は符号なし。 */
export function fmtDb(db) {
  if (!(db > GAIN_MIN_DB)) return '−∞';
  const a = Math.abs(db);
  return `${db > 0.04 ? '+' : db < -0.04 ? '−' : ''}${a.toFixed(1)}`;
}

/** パン（−1〜1）→ 画面の値（−100〜100 の整数）。 */
export const panUi = (pan) => Math.round(clamp(+pan || 0, -1, 1) * 100);
/** 画面の値 → エンジンの値（小数 4 桁。エンジンの丸めと同じ）。 */
export const panFromUi = (ui) => Math.round(clamp(ui, -100, 100) * 100) / 10000;

export function fmtPan(pan) {
  const u = panUi(pan);
  return u === 0 ? 'C' : `${u < 0 ? 'L' : 'R'} ${Math.abs(u)}`;
}

export function panSpeech(pan) {
  const u = panUi(pan);
  return u === 0 ? '中央' : `${u < 0 ? '左' : '右'} ${Math.abs(u)}`;
}

/** パンのノブの SVG（7 時〜5 時。中央から値の側へ弧）。 */
export function knobSvg(pan) {
  const f = (v) => (Math.round(v * 10) / 10).toString();
  const a = clamp(panUi(pan), -100, 100) / 100 * 135;
  const r = 6; const cx = 8; const cy = 8;
  const pt = (deg) => [cx + r * Math.sin(deg * Math.PI / 180), cy - r * Math.cos(deg * Math.PI / 180)];
  const arc = (d0, d1) => {
    const [x0, y0] = pt(d0); const [x1, y1] = pt(d1);
    return `M${f(x0)},${f(y0)}A${r},${r} 0 ${Math.abs(d1 - d0) > 180 ? 1 : 0} ${d1 > d0 ? 1 : 0} ${f(x1)},${f(y1)}`;
  };
  const [ix, iy] = pt(a);
  const ix0 = cx + 2 * Math.sin(a * Math.PI / 180); const iy0 = cy - 2 * Math.cos(a * Math.PI / 180);
  return `<svg viewBox="0 0 16 16" aria-hidden="true"><path class="bg" d="${arc(-135, 135)}"/>`
    + `${Math.abs(a) > 1 ? `<path class="arc" d="${arc(0, a)}"/>` : ''}`
    + `<line class="ind" x1="${f(ix0)}" y1="${f(iy0)}" x2="${f(ix)}" y2="${f(iy)}"/></svg>`;
}

/** トラックの音量（dB）。無い・壊れていれば 0。 */
export const gainOf = (t) => (Number.isFinite(+t?.gain_db) ? clamp(+t.gain_db, GAIN_MIN_DB, GAIN_MAX_DB) : 0);
/** トラックのパン（−1〜1）。無い・壊れていれば 0。 */
export const panOf = (t) => (Number.isFinite(+t?.pan) ? clamp(+t.pan, -1, 1) : 0);
