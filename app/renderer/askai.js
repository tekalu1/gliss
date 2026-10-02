// 右クリックの「AI に頼む」: 選んだノート（か、ループの範囲）を AI に渡す書き出しの文をクリップボードに入れる。
//   「Gliss（gliss）で、今開いている曲の take1 トラックの n004〜n009（1:24.30〜1:26.10、6 ノート）について:」
// 続きはユーザーが AI のチャットに書く。AI はこの文から load_project()（引数なし）で同じ曲を開き、
// ノートの id と秒（エンジンのツールと同じ、トラックの頭が 0 の編集前の秒）で対象を見つける。
import { S, isSel, spanOf, toSource } from './state.js';
import { status } from './engine.js';
import { toast } from './aidlg.js';

const LIST_MAX = 6;            // 飛び飛びのノートを 1 つずつ並べるのはこの数まで

/** 対象のノート（選択。無ければループの範囲に掛かるノート）。並びは時間順。 */
export function askTargets() {
  let ns = S.pitched.filter((n) => isSel(n.id));
  if (!ns.length && S.loop) {
    const a = S.loop[0] - S.off; const b = S.loop[1] - S.off;
    ns = S.pitched.filter((n) => { const [s0, s1] = spanOf(n); return s1 > a && s0 < b; });
  }
  return ns.sort((x, y) => x.start_sec - y.start_sec);
}

export function canAskAi() {
  return !!S.vd && (askTargets().length > 0 || !!S.loop);
}

/** 1:24.30（分:秒.百分の一秒）。 */
function fmt(t) {
  const c = Math.round(Math.max(0, t) * 100);
  const m = Math.floor(c / 6000); const s = (c % 6000) / 100;
  return `${m}:${s.toFixed(2).padStart(5, '0')}`;
}

/** ノートの id の書き方: 1 つ・続いている（n004〜n009）・飛び飛び（n004・n007・n009）。 */
function idsText(ns) {
  if (ns.length === 1) return ns[0].id;
  const idx = new Map(S.pitched.map((n, i) => [n.id, i]));
  const run = ns.every((n, k) => k === 0 || idx.get(n.id) === idx.get(ns[k - 1].id) + 1);
  if (run || ns.length > LIST_MAX) return `${ns[0].id}〜${ns[ns.length - 1].id}`;
  return ns.map((n) => n.id).join('・');
}

/** クリップボードに入れる文（対象が無ければ null）と、知らせる短い文。 */
export function askText() {
  const ns = askTargets();
  const cur = S.tracks.find((t) => t.id === S.session?.current);
  const track = cur?.name || S.take?.name || '';
  const where = track ? `今開いている曲の ${track} トラックの ` : '今開いている曲の ';
  if (ns.length) {
    const t0 = Math.min(...ns.map((n) => n.start_sec)); const t1 = Math.max(...ns.map((n) => n.end_sec));
    return { text: `Gliss（gliss）で、${where}${idsText(ns)}（${fmt(t0)}〜${fmt(t1)}、${ns.length} ノート）について:`,
      note: `${ns.length} ノートをコピー` };
  }
  if (!S.loop) return null;
  // ノートの無い範囲（ループ）: 秒だけ。エンジンの秒（トラックの頭が 0、編集前）に直す
  const a = toSource(S.loop[0] - S.off); const b = toSource(S.loop[1] - S.off);
  return { text: `Gliss（gliss）で、${where}${fmt(a)}〜${fmt(b)} について:`, note: '範囲をコピー' };
}

export async function askAi() {
  const r = askText();
  if (!r) { status('AI に頼む: ノートか範囲を選んでから'); return null; }
  await window.api.copyText(r.text);
  toast(r.note);
  status(r.note);
  return r;
}
