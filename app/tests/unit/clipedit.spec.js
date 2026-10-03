// クリップの分割・部分のミュートの計算（renderer/clipedit.js。エンジンの session.py と同じ規則）。Electron は起動しない。
//
//   (C1) 切れ目・消した区間の整え方（範囲・昇順・近いものは 1 つに・つながる区間は 1 つに）
//   (C2) 部分を消す／戻す・つなぐ（片側だけ消えていれば戻る）
//   (C3) 再生の gain の予約（区間の中は 0・フェードは外側 5 ms・区間の途中から鳴らし始める）
import { test, expect } from '@playwright/test';

import {
  CLIP_FADE, covered, gainRegions, joinAt, normCuts, normMutes, paintPiece, pieces, planClipGain, subtractRange,
} from '../../renderer/clipedit.js';

test('(C1) 切れ目・消した区間を整える', () => {
  expect(normCuts([2, 0, -1, 1, 1.0004, 9.9, NaN], 3)).toEqual([1, 2]);
  expect(normMutes([[1, 2], [2.0004, 3], [5, 4], [0.5, 0.5], [-1, 0.5]], 3)).toEqual([[0, 0.5], [1, 3]]);
  expect(subtractRange([[0, 3]], 1, 2)).toEqual([[0, 1], [2, 3]]);
  expect(subtractRange([[0, 1]], 1, 2)).toEqual([[0, 1]]);
  expect(pieces([1, 2], 3)).toEqual([[0, 1], [1, 2], [2, 3]]);
  expect(covered([[1, 2]], 1, 2)).toBe(true);
  expect(covered([[1, 1.5]], 1, 2)).toBe(false);
});

test('(C2) 部分を消す／戻す・つなぐ', () => {
  const t = { duration_sec: 3, cuts: [1, 2], mutes: [] };
  t.mutes = paintPiece(t, 1, true);
  expect(t.mutes).toEqual([[1, 2]]);
  t.mutes = paintPiece(t, 2, true);
  expect(t.mutes).toEqual([[1, 3]]);                  // 隣の部分も消すと 1 つの区間
  t.mutes = paintPiece(t, 1, false);
  expect(t.mutes).toEqual([[2, 3]]);
  // つなぐ: 片側だけ消えていれば戻す。両側が消えていれば消したまま
  expect(joinAt(t, 2)).toEqual({ cuts: [1], mutes: [] });
  t.mutes = [[1, 3]];
  expect(joinAt(t, 2)).toEqual({ cuts: [1], mutes: [[1, 3]] });
});

function fakeParam() {
  const ev = [];
  return { ev, setValueAtTime: (v, t) => ev.push(['set', v, +t.toFixed(6)]), linearRampToValueAtTime: (v, t) => ev.push(['ramp', v, +t.toFixed(6)]) };
}

test('(C3) 再生の gain の予約: 区間の中は 0・フェードは外側', () => {
  const regions = gainRegions([[1, 2]], 0.5);          // タイムラインでは 1.5〜2.5
  expect(regions).toEqual([[1.5, 2.5]]);
  const p = fakeParam();
  planClipGain(p, regions, 10, 0, 4);                   // タイムラインの 0〜4 を ctx の 10 秒から
  expect(p.ev).toEqual([
    ['set', 1, 10],
    ['set', 1, +(11.5 - CLIP_FADE).toFixed(6)], ['ramp', 0, 11.5],      // 区間の手前 5 ms で 1 → 0
    ['set', 0, 12.5], ['ramp', 1, +(12.5 + CLIP_FADE).toFixed(6)],      // 区間の後ろ 5 ms で 0 → 1
  ]);
  // 区間の途中から鳴らし始める: 最初から 0。終わりで戻す
  const q = fakeParam();
  planClipGain(q, regions, 20, 2.0, 4);
  expect(q.ev[0]).toEqual(['set', 0, 20]);
  expect(q.ev[1]).toEqual(['set', 0, 20.5]);
  // 区間の外だけを鳴らす: 予約は初期値だけ
  const r = fakeParam();
  planClipGain(r, regions, 0, 3, 4);
  expect(r.ev).toEqual([['set', 1, 0]]);
  // フェードが重なるほど近い区間は 1 つにつなぐ
  expect(gainRegions([[1, 2], [2.004, 3]], 0)).toEqual([[1, 3]]);
  expect(gainRegions([[1, 2], [2.1, 3]], 0)).toEqual([[1, 2], [2.1, 3]]);
});
