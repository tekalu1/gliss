// 色の定義はここ 1 か所（2026-10-10 承認。docs/guide-coverage.md「色の規則」）。
//
// 画面の JS（state.js の COLORS・corr.js の補正の色）も、CSS の変数（--take など。applyCssVars が :root に入れる）も、
// ここから引く。色を変えるときはここだけを直す（index.html に色の 16 進を重ねて書かない）。
//
//   今のテイク = 黄（帯 = タイミング・線 = ピッチ）。自動補正 = 黄 → 橙 → 赤。手で直した = 白（塗り＋白の縁）
//   ガイド = 青（薄い塗り＋1px の青の縁・ピッチの線も青）。AI が直したノート = 紫の縁
//   塗り・縁・線の別でも見分けられる形にして、色覚の差に頼りすぎない。

export const COLORS = {
  TAKE: '#e6d24a',
  // ガイド（帯の縁・ピッチの線・トラックビューの波形）と、ホバー・選択したノートと対のガイドの縁
  GUIDE: '#5aa2ff', GUIDE_HI: '#bcd9ff',
  SEL: '#f2f2f2',
  // 選択したノートの元の長さ（帯の上の細い線と両端の縦線）
  WAS: '#a4a4aa',
  // AI（Claude Code など）の編集が最後に当たっているノートの縁
  AI: '#c08cff',
  // 伴奏（トラックビューの波形。背景に近い薄いグレー）・編集中でないボーカル（暗い黄）
  INST: '#707076', VOCAL: '#7d7437',
  // 子音（歌詞があるときだけ）: テイクの黄と同じ色相・明るさのまま、彩度だけ落とす
  CONS: '#bdb57a',
};

// 自動補正（ガイドに合わせる）の度合い 0〜1: 黄（テイク）→ 橙 → 赤。段ごとに暗くなる（相対輝度 0.63 → 0.45 → 0.28 → 0.16）
export const RAMP = ['#e6d24a', '#eaa73c', '#e3702e', '#d23a2a'];
// 手動補正（ドラッグ・スライダー・ペン）
export const MANUAL = '#ffffff';
// 無音のノートのピッチの線（補正の色にしない）
export const MUTED_LINE = '#5c5c62';

// 塗りの濃さ・縁の太さ（draw.js と index.html の CSS が同じ値を引く）
export const GUIDE_LOOK = {
  fill: 0.16,          // ガイドの帯の塗り
  fillDim: 0.08,       // 「ガイドに合わせる」を開いている間の、対応しないガイドの帯
  fillPair: 0.3,       // ホバー・選択したノートと対のガイドの帯
  edge: 0.9, edgeDim: 0.3, edgeWidth: 1, edgePairWidth: 1.8,
  lineWidth: 1.4,
};

// :root の CSS 変数（--take など）。index.html はこれを引く
export const CSS_VARS = {
  '--take': COLORS.TAKE, '--guide': COLORS.GUIDE, '--guide-hi': COLORS.GUIDE_HI, '--sel': COLORS.SEL,
  '--ai': COLORS.AI, '--manual': MANUAL, '--was': COLORS.WAS,
  // 失敗・警告の橙（ノートの色ではない）
  '--orig': '#cf7a2e',
};

export function applyCssVars(el = globalThis.document?.documentElement) {
  if (!el?.style?.setProperty) return;
  for (const [k, v] of Object.entries(CSS_VARS)) el.style.setProperty(k, v);
}

// 画面で読んだときに 1 度だけ入れる（Node の単体試験では document が無い・代用品のことがあるので、無ければ何もしない）
applyCssVars();
