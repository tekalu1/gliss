// ピアノロールの色の凡例（2026-10-10 承認）。初回は出し、× で閉じたら覚える。表示メニューの「色の凡例を表示」で出し入れする。
//
// 色は palette.js から引く（ここに 16 進を書かない）。DOM の部品なので、draw.js の SVG とは別に重ねる（ポインターの操作を奪わない）。
import { COLORS, GUIDE_LOOK, MANUAL, RAMP } from './palette.js';
import { S } from './state.js';

export const L = { show: true };

const hex2rgba = (h, a) => `rgba(${[1, 3, 5].map((k) => parseInt(h.slice(k, k + 2), 16)).join(',')},${a})`;

// [名前, 見本の style（塗り・縁・線の形も変える。色覚の差に頼りすぎない）]
const ITEMS = [
  ['今のテイク', `background:${hex2rgba(COLORS.TAKE, 0.45)};box-shadow:inset 0 -3px 0 ${COLORS.TAKE}`],
  ['手で直した', `background:${hex2rgba(MANUAL, 0.45)};border:1px solid ${hex2rgba(MANUAL, 0.55)};box-shadow:inset 0 -3px 0 ${MANUAL}`],
  ['自動補正', `background:linear-gradient(90deg,${RAMP.join(',')})`],
  ['AI が直した', `background:${hex2rgba(COLORS.TAKE, 0.45)};border:2px solid ${COLORS.AI}`],
  ['ガイド', `background:${hex2rgba(COLORS.GUIDE, GUIDE_LOOK.fill)};border:${GUIDE_LOOK.edgeWidth}px solid ${hex2rgba(COLORS.GUIDE, GUIDE_LOOK.edge)}`],
];

/** 設定を読む（起動時。main の bootstrap の legend）。 */
export function loadLegend(v) {
  L.show = v !== false;
  syncLegend();
}
function save() {
  window.api?.saveState?.({ legend: L.show });
}

/** 出す・隠す（メニュー・× のボタン）。ユーザー設定に残す。 */
export function setLegend(on) {
  L.show = !!on;
  save();
  syncLegend();
}

let el = null;
function ensure() {
  if (el) return el;
  const host = document.querySelector('.mock');
  if (!host) return null;
  el = document.createElement('div');
  el.id = 'legend';
  el.className = 'legend';
  el.setAttribute('role', 'group');
  el.setAttribute('aria-label', '色の凡例');
  el.innerHTML = ITEMS.map(([name, style]) => `<span class="lg"><i style="${style}"></i>${name}</span>`).join('')
    + '<button type="button" class="lg-x" aria-label="凡例を閉じる" title="凡例を閉じる（表示メニューでもう一度出せる）">×</button>';
  el.querySelector('.lg-x').addEventListener('click', () => setLegend(false));
  host.appendChild(el);
  return el;
}

/** 表示・非表示を状態に合わせる（曲が無いうちは出さない）。 */
export function syncLegend() {
  if (typeof document === 'undefined') return;
  const e = ensure();
  if (e) e.hidden = !(L.show && S.vd);
}
