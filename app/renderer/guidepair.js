// ホバー・選択したノートと対になるガイドのノート、と その差の文（2026-10-10 承認。ピアノロールの色）。
//
// 対 = 時間がいちばん長く重なる、音程のあるガイドのノート（エンジンの対応付けとは別。見るためだけの目安）。
// 差 = 帯の高さ（平均の音程）の差 cent と、頭の時刻の差 ms。ともにガイドを基準にしたテイクの向き。
import { S, bandOf, guideBandOf, spanOf } from './state.js';

/** ノート id → 対のガイドのノート（無ければ null）。 */
export function guidePairOf(id) {
  const n = id && S.byId.get(id);
  const gs = S.vd?.guide_notes;
  if (!n || n.kind !== 'note' || !gs?.length) return null;
  const [a, b] = spanOf(n);
  let best = null; let bestOv = 0;
  for (const g of gs) {
    if (g.pitch_midi == null) continue;
    const ov = Math.min(b, g.end_sec) - Math.max(a, g.start_sec);
    if (ov > bestOv) { best = g; bestOv = ov; }
  }
  return best;
}

/** 「ガイドより +32 cent・40 ms 遅い」。対が無ければ null。 */
export function guideDiffText(id) {
  const n = S.byId.get(id);
  const g = guidePairOf(id);
  if (!n || !g) return null;
  const dc = Math.round((bandOf(n) - guideBandOf(g)) * 100);
  const dt = Math.round((spanOf(n)[0] - g.start_sec) * 1000);
  const cent = dc === 0 ? 'ガイドと同じ高さ' : `ガイドより ${dc > 0 ? '+' : '−'}${Math.abs(dc)} cent`;
  const time = Math.abs(dt) < 5 ? '同じ時刻' : `${Math.abs(dt)} ms ${dt > 0 ? '遅い' : '早い'}`;
  return `${cent}・${time}`;
}
