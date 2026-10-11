// 「ガイドに合わせる」の割合の式（renderer/strength.js）が、エンジン（project/strength.py）と同じ線を出す。
// Electron は起動しない。
//
//   fixtures/strength-plan.json は合成の歌声（engine/tests/test_strength.py と同じ）から作った計画と、
//   割合ごとのエンジンの値・当てた後の view の線。fresh = 記録の無いノートだけ、mixed = 70% と 30% の
//   ノートが続き、最後のノートを手で直した（+40 セント）。
//   (S1) 作り直すノートと割合が、エンジンの decide と同じ
//   (S2) 作り直すフレームの値が、エンジンの preview_values と同じ
//   (S3) プレビュー（当てる前の view + 差）が、当てた後の view と同じ（手で直した分は差で残る）
//   (S4) ポップアップの値: 手で直したノートを既定で除く・混在・記録の無いノートは 0%
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

import { decide, parseMaterials, strengthDelta, strengthFrames, strengthSummary } from '../../renderer/strength.js';

const FX = JSON.parse(fs.readFileSync(path.join(import.meta.dirname, 'fixtures', 'strength-plan.json'), 'utf8'));

for (const sc of FX.scenarios) {
  test.describe(sc.name, () => {
    const mats = parseMaterials(sc.strength);
    for (const c of sc.cases) {
      const label = JSON.stringify(c.params);
      test(`(S1)(S2) ${label}`, () => {
        const made = decide(sc.strength.notes, mats, c.params);
        expect(Object.fromEntries([...made].map(([k, v]) => [k, v]))).toEqual(c.made);
        const { values } = strengthFrames(sc.strength, mats, c.params);
        const want = Object.entries(c.values);
        expect(values.size).toBe(want.length);
        for (const [i, v] of want) expect(Math.abs(values.get(+i).v - v)).toBeLessThan(1e-5);
      });
      test(`(S3) ${label}`, () => {
        const delta = strengthDelta(sc.strength, mats, c.params, null, (i) => sc.before[i]);
        // 手で直したノート（+40 セント）の頭の 30 ms 手前まで: その移しのつなぎがかかる。線を外す（0%）と
        // 補正前の線（合わせたときの値）に戻して描くので、そのつなぎの分だけずれる（当てた後は正しい）
        const manual = Object.entries(sc.strength.notes).filter(([, r]) => r.manual).map(([n]) => n);
        const heads = manual.flatMap((n) => Object.values(mats).flatMap((m) => (m.byNote.get(n) || []).map((k) => m.segs[k].s0)));
        let worst = 0; let near = 0;
        sc.before.forEach((b, i) => {
          const a = c.after[i];
          if (a == null || b == null) return;
          const e = Math.abs(b + (delta.get(i)?.d || 0) - a);
          if (heads.some((t) => t - i * FX.hop > 0 && t - i * FX.hop < 0.03)) near = Math.max(near, e);
          else worst = Math.max(worst, e);
        });
        // 当てた鉛筆は点を線でつなぐ・view は 1/100000 半音に丸める
        expect(worst).toBeLessThan(2e-3);
        expect(near).toBeLessThan(0.1);
      });
    }
  });
}

test('(S3) 当てた割合からの差（ポップアップで 2 回目に動かした）', () => {
  const sc = FX.scenarios[0];
  const mats = parseMaterials(sc.strength);
  const [c1, c2] = sc.cases;
  const delta = strengthDelta(sc.strength, mats, c2.params, c1.params, (i) => sc.before[i]);
  let worst = 0;
  c1.after.forEach((b, i) => {
    const a = c2.after[i];
    if (a == null || b == null) return;
    worst = Math.max(worst, Math.abs(b + (delta.get(i)?.d || 0) - a));
  });
  expect(worst).toBeLessThan(2e-3);
});

test('(S4) ポップアップの値', () => {
  const mixed = FX.scenarios[1].strength;
  const ids = FX.ids;
  const s = strengthSummary(mixed);
  expect(s.manual).toEqual([ids.at(-1)]);
  expect(s.center.mixed).toBe(true);
  expect(s.center.min).toBeCloseTo(0.3);
  expect(s.center.max).toBeCloseTo(0.7);
  expect(s.stale).toEqual([]);
  const fresh = strengthSummary(FX.scenarios[0].strength);
  expect(fresh.center).toEqual({ mixed: false, min: 0, max: 0, mean: 0 });
  // 変えられないノートは数えず理由を出す。「今の状態で合わせ直す」（restart）なら 0% から数える
  const stale = structuredClone(mixed);
  Object.assign(stale.notes[ids[1]], { state: 'stale', reason: 'テイクを解析し直した' });
  const s2 = strengthSummary(stale);
  expect(s2.stale).toEqual([ids[1]]);
  expect(s2.reasons).toEqual(['テイクを解析し直した']);
  expect(s2.center.min).toBeCloseTo(0.7);
  const s3 = strengthSummary(stale, { restart: [ids[1]] });
  expect(s3.stale).toEqual([]);
  expect(s3.center.min).toBe(0);
});
