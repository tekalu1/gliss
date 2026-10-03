// トラックの音量・パンの目盛りと表記（renderer/mixer.js）。Electron は起動しない。
//
//   (M1) スライダーの位置 ⇄ dB: 0 dB が 80%、右端が +6 dB、左が −∞（−60 dB 以下）。行って戻して同じ
//   (M2) 表記: 「−3.5」「+6.0」「−∞」、パンは「C」「L 30」「R 100」と読み上げ「中央」「左 30」
//   (M3) 壊れた値（無い・数でない・範囲の外）は既定値・端に丸める
import { test, expect } from '@playwright/test';
import {
  GAIN_MAX_DB, GAIN_MIN_DB, ZERO_POS, dbToGain, dbToPos, fmtDb, fmtPan, gainOf, knobSvg, panFromUi, panOf, panSpeech, panUi, posToDb,
} from '../../renderer/mixer.js';

test('(M1) スライダーの位置と dB', () => {
  expect(ZERO_POS).toBe(0.8);
  expect(posToDb(0.8)).toBe(0);
  expect(posToDb(1)).toBeCloseTo(GAIN_MAX_DB, 3);
  expect(posToDb(0)).toBe(GAIN_MIN_DB);
  expect(posToDb(0.05)).toBe(GAIN_MIN_DB);                     // 左のほうは無音（−∞）
  expect(dbToPos(0)).toBeCloseTo(0.8, 6);
  expect(dbToPos(GAIN_MAX_DB)).toBeCloseTo(1, 6);
  expect(dbToPos(GAIN_MIN_DB)).toBe(0);
  expect(dbToPos(-200)).toBe(0);
  expect(dbToPos(40)).toBe(1);
  // 3 乗に近い: 位置が半分（40%）で 約 −18.6 dB（線形の増幅率は (0.5)^N）
  expect(posToDb(0.4)).toBeCloseTo(-18.6, 1);
  // 行って戻して同じ
  for (const db of [-50, -20, -7.7, -1, 0, 0.5, 3, 6]) expect(posToDb(dbToPos(db))).toBeCloseTo(db, 2);
  // 位置が増えれば dB も増える
  let prev = -Infinity;
  for (let p = 0.1; p <= 1; p += 0.05) { const d = posToDb(p); expect(d).toBeGreaterThan(prev); prev = d; }
  // 線形の増幅率
  expect(dbToGain(0)).toBe(1);
  expect(dbToGain(-6)).toBeCloseTo(0.5012, 4);
  expect(dbToGain(6)).toBeCloseTo(1.9953, 4);
  expect(dbToGain(GAIN_MIN_DB)).toBe(0);
});

test('(M2) 表記と読み上げ', () => {
  expect(fmtDb(0)).toBe('0.0');
  expect(fmtDb(0.02)).toBe('0.0');
  expect(fmtDb(-3.5)).toBe('−3.5');
  expect(fmtDb(6)).toBe('+6.0');
  expect(fmtDb(0.5)).toBe('+0.5');
  expect(fmtDb(GAIN_MIN_DB)).toBe('−∞');
  expect(fmtDb(-90)).toBe('−∞');
  expect(fmtPan(0)).toBe('C');
  expect(fmtPan(0.004)).toBe('C');
  expect(fmtPan(-0.3)).toBe('L 30');
  expect(fmtPan(1)).toBe('R 100');
  expect(panSpeech(0)).toBe('中央');
  expect(panSpeech(-0.3)).toBe('左 30');
  expect(panSpeech(0.15)).toBe('右 15');
  expect(panUi(-1)).toBe(-100);
  expect(panUi(0.333)).toBe(33);
  expect(panFromUi(15)).toBe(0.15);
  expect(panFromUi(12.3)).toBe(0.123);
  expect(panFromUi(250)).toBe(1);
  expect(panFromUi(-250)).toBe(-1);
  // ノブ: 中央は弧を描かない。左右で指針の向きが逆
  expect(knobSvg(0)).not.toContain('class="arc"');
  expect(knobSvg(0.5)).toContain('class="arc"');
  const x2 = (svg) => +svg.match(/class="ind" x1="[-\d.]+" y1="[-\d.]+" x2="([-\d.]+)"/)[1];
  expect(x2(knobSvg(-0.5))).toBeLessThan(8);
  expect(x2(knobSvg(0.5))).toBeGreaterThan(8);
  expect(x2(knobSvg(0))).toBe(8);
});

test('(M3) 壊れた値は既定値・端に丸める', () => {
  expect(gainOf({})).toBe(0);
  expect(gainOf({ gain_db: 'x' })).toBe(0);
  expect(gainOf({ gain_db: -3 })).toBe(-3);
  expect(gainOf({ gain_db: 99 })).toBe(GAIN_MAX_DB);
  expect(gainOf({ gain_db: -999 })).toBe(GAIN_MIN_DB);
  expect(gainOf(undefined)).toBe(0);
  expect(panOf({})).toBe(0);
  expect(panOf({ pan: 'x' })).toBe(0);
  expect(panOf({ pan: 7 })).toBe(1);
  expect(panOf({ pan: -0.25 })).toBe(-0.25);
});
