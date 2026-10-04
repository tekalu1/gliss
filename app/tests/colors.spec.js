// 補正の度合いを色で見せる・元の長さ（issue #37。モック proposal/v4.html、2026-09-26 の決定）。
//
//   (1) 補正なし: 帯・線は黄。ガイドは濃いグレー（線 1.4 px 不透明・帯は薄く）。橙の編集前の線・斜めの線は無い。
//       トラックビューの波形もガイドは同じ濃いグレー、伴奏は薄いグレー
//   (2) ピッチのドラッグ = 手動: その線が白。ドラッグ中の色 = 離した後の色
//   (3) 端のドラッグ = 手動: 帯が白（接続された隣も）。ドラッグ中 = 離した後
//   (4) 元の長さ: 選択したノートだけ、帯の上に細いグレーの線と両端の縦線
//   (5) ガイドに合わせる = 自動: 度合いに応じて黄 → 赤。スライダー中 = 確定後。最後にかけた方（自動）になる
//   (6) 取り消すと手動の白に戻る
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const TAKE = M.clip('C');
const GUIDE = M.clip('C2');
const PROJECT = path.join(REPO, 'projects', '_test-colors');
const USERDATA = `${PROJECT}-userdata`;
const TAKE_C = '#e6d24a';
const GUIDE_C = '#4e4e54';
const WHITE = '#ffffff';
const SHOTS = path.join(REPO, 'scratchpad');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  fs.rmSync(PROJECT, { recursive: true, force: true });
  fs.rmSync(USERDATA, { recursive: true, force: true });
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--guide', GUIDE, '--project-dir', PROJECT,
      '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await win.evaluate(() => { window.__app.S.view = { t0: 0, span: window.__app.totalSec() }; window.__app.render(); });
});

test.afterAll(async () => {
  await app?.close();
});

const settle = () => win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
const corr = () => win.evaluate(() => window.__app.corr());
const drawn = () => win.evaluate(() => window.__app.drawnColors());
const byId = (xs) => Object.fromEntries(xs.map((x) => [x.id, x]));

test('(1) 補正なしは黄、ガイドは濃いグレー。橙の線・斜めの線は無い', async () => {
  const c = await corr();
  expect(c.every((n) => n.band === TAKE_C && n.line === TAKE_C)).toBe(true);
  const d = await drawn();
  expect(new Set(Object.values(d.blobs))).toEqual(new Set([TAKE_C]));
  expect(new Set(d.lines)).toEqual(new Set([TAKE_C]));
  expect(d.orig).toEqual([]);
  const look = await win.evaluate(() => {
    const q = (s) => [...document.querySelectorAll(s)];
    return {
      guideBlobs: q('#roll path[data-guide]').map((p) => [p.getAttribute('fill'), p.getAttribute('opacity')]),
      guideLines: q('#roll path[data-guide-line]').map((p) => [p.getAttribute('stroke'), p.getAttribute('stroke-width'), p.getAttribute('opacity')]),
      orange: document.querySelector('#roll').innerHTML.includes('#cf7a2e'),
      lanes: q('#lanes path').map((p) => p.getAttribute('fill')),
      rollRows: [...new Set(q('#roll rect[x="44"]').map((r) => r.getAttribute('fill')))],
      guideVariable: getComputedStyle(document.documentElement).getPropertyValue('--guide').trim(),
    };
  });
  expect(look.guideBlobs.length).toBeGreaterThan(0);
  expect(new Set(look.guideBlobs.map((g) => g.join(' ')))).toEqual(new Set([`${GUIDE_C} .55`]));
  expect(look.guideLines).toEqual([[GUIDE_C, '1.4', null]]);
  expect(look.rollRows).toEqual(expect.arrayContaining(['#0d0d0f', '#121215']));
  expect(look.guideVariable).toBe(GUIDE_C);
  expect(look.orange).toBe(false);
  // トラックビュー: 編集中は黄、ガイドは同じ濃いグレー
  expect(look.lanes).toContain(TAKE_C);
  expect(look.lanes).toContain(GUIDE_C);
});

test('(2) ピッチのドラッグ = 手動の白。ドラッグ中 = 離した後', async () => {
  const ns = await win.evaluate(() => window.__app.notes());
  const target = ns.filter((n) => n.end - n.start > 0.15)[1] || ns[1];
  const box = await win.locator(`#roll rect[data-note="${target.id}"]:not([data-edge])`).first().boundingBox();
  await win.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await win.mouse.down();
  await win.mouse.move(box.x + box.width / 2, box.y + box.height / 2 - 30, { steps: 6 });
  const during = byId(await corr());
  expect(during[target.id].pitch.manual).toBe(true);
  expect(during[target.id].line).toBe(WHITE);
  expect((await drawn()).lines).toContain(WHITE);
  await win.mouse.up();
  await settle();
  const after = byId(await corr());
  expect(after[target.id].engPitch.manual).toBe(true);
  for (const id of Object.keys(after)) expect(after[id].line).toBe(during[id].line);
  // タイミングは動かしていない
  expect(after[target.id].band).toBe(TAKE_C);
  fs.mkdirSync(SHOTS, { recursive: true });
  await win.screenshot({ path: path.join(SHOTS, 'issue45-manual-guide.png') });
});

let edgeNote = null;
test('(3) 端のドラッグ = 手動の白（接続された隣も）。ドラッグ中 = 離した後', async () => {
  const ns = await win.evaluate(() => window.__app.notes());
  const k = ns.findIndex((n, i) => i + 1 < ns.length && Math.abs(ns[i + 1].start - n.end) < 1e-6
    && n.end - n.start > 0.2 && n.cents === 0);
  expect(k).toBeGreaterThanOrEqual(0);
  edgeNote = ns[k].id;
  const box = await win.locator(`#roll rect[data-note="${edgeNote}"][data-edge="end"]`).first().boundingBox();
  await win.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await win.mouse.down();
  await win.mouse.move(box.x + box.width / 2 + 30, box.y + box.height / 2, { steps: 8 });
  await win.waitForFunction(() => Math.abs(window.__app.plan()?.x || 0) > 0.005, null, { timeout: 30000 });
  const during = byId(await corr());
  expect(during[edgeNote].band).toBe(WHITE);
  expect(during[ns[k + 1].id].band).toBe(WHITE);
  expect((await drawn()).blobs[edgeNote]).toBe(WHITE);
  await win.mouse.up();
  await settle();
  const after = byId(await corr());
  expect(after[edgeNote].engTiming.manual).toBe(true);
  for (const id of Object.keys(after)) expect(after[id].band).toBe(during[id].band);
});

test('(4) 元の長さは選択したノートだけ、帯の上の細いグレーの線（両端に縦線）', async () => {
  expect((await drawn()).orig).toEqual([]);
  await win.evaluate((id) => { window.__app.S.sel = [id]; window.__app.render(); }, edgeNote);
  const d = await drawn();
  expect(d.orig.length).toBe(1);
  expect(d.orig[0].id).toBe(edgeNote);
  expect(d.orig[0].stroke).toBe('#a4a4aa');
  // M x0 y+5 V y H x1 V y+5（寸法線の形）。x0 / x1 = 元の頭・尻
  const m = d.orig[0].d.match(/^M(-?[\d.]+) (-?[\d.]+)V(-?[\d.]+)H(-?[\d.]+)V(-?[\d.]+)$/);
  expect(m).not.toBeNull();
  const want = await win.evaluate((id) => {
    const S = window.__app.S; const n = S.byId.get(id);
    const W = document.querySelector('#roll').getBoundingClientRect().width;
    const X = (t) => 44 + (t - S.view.t0) / S.view.span * (W - 44);
    return [X(n.start_sec), X(n.end_sec), X(n.edited_end_sec)];
  }, edgeNote);
  expect(Math.abs(+m[1] - want[0])).toBeLessThan(0.2);
  expect(Math.abs(+m[4] - want[1])).toBeLessThan(0.2);
  expect(want[2] - want[1]).toBeGreaterThan(5);        // 動かした分だけ帯の尻が線の端より右
  expect(+m[2] - +m[3]).toBeCloseTo(5, 1);
  await win.evaluate(() => { window.__app.S.sel = []; window.__app.render(); });
  expect((await drawn()).orig).toEqual([]);
});

test('形の計画に差分フレームがないノートは元の線色を保つ', async () => {
  const before = byId(await corr());
  const { rows, changed } = await win.evaluate((yellowIds) => {
    const S = window.__app.S;
    const saved = S.plan;
    const f0 = S.vd.f0;
    const target = S.notes.find((n) => n.kind === 'note' && yellowIds.includes(n.id)
      && f0.take_edited_midi.some((v, i) => v != null
        && f0.t0_sec + i * f0.hop_sec >= n.start_sec
        && f0.t0_sec + i * f0.hop_sec < n.end_sec));
    const index = f0.take_edited_midi.findIndex((v, i) => v != null
      && f0.t0_sec + i * f0.hop_sec >= target.start_sec
      && f0.t0_sec + i * f0.hop_sec < target.end_sec);
    try {
      S.plan = {
        data: { kind: 'guide', knots: [], pieces: [], params: { match_pitch_shape: true },
          pitch: Object.fromEntries(S.notes.map((n) => [n.id, 100])) },
        pitch: 0.7, pitch0: 0, x: 0, x0: 0, shapeFrames: new Map([[index, [100, 200, 1]]]),
        guides: new Set(), takes: new Set(),
      };
      window.__app.render();
      return { rows: window.__app.corr(), changed: target.id };
    } finally {
      S.plan = saved;
      window.__app.render();
    }
  }, Object.values(before).filter((n) => n.line === TAKE_C).map((n) => n.id));
  const preview = byId(rows);
  expect(preview[changed].line).not.toBe(before[changed].line);
  for (const [id, n] of Object.entries(before)) {
    if (id === changed) continue;
    expect(preview[id].line, id).toBe(n.line);
    expect(preview[id].pitch, id).toEqual(n.pitch);
  }
});

test('(5) ガイドに合わせる = 自動（黄 → 赤）。スライダー中 = 確定後', async () => {
  await win.locator('#bMacro').click();
  await expect(win.locator('#pop')).toBeVisible();
  await win.locator('#popPitchShape').uncheck();
  await win.waitForFunction(() => window.__app.S.plan?.data?.params?.match_pitch_shape === false, null, { timeout: 60000 });
  await win.evaluate(() => {
    for (const [id, v] of [['#popPitch', '30'], ['#popTime', '100']]) {
      const el = document.querySelector(id);
      el.value = v;
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
  const during = byId(await corr());
  const autoP = Object.values(during).filter((n) => n.pitch && !n.pitch.manual);
  const autoT = Object.values(during).filter((n) => n.timing && !n.timing.manual);
  expect(autoP.length).toBeGreaterThan(2);
  expect(autoT.length).toBeGreaterThan(0);
  // 度合いが違えば色も違う（連続）。度合い 0 = 黄、大きいほど赤に近い
  const degs = autoP.map((n) => n.pitch.degree);
  expect(Math.max(...degs)).toBeGreaterThan(Math.min(...degs));
  for (const n of autoP) expect(n.line).not.toBe(WHITE);
  await win.evaluate(() => {
    document.querySelector('#popTime').dispatchEvent(new Event('change', { bubbles: true }));
  });
  await settle();
  const after = byId(await corr());
  for (const id of Object.keys(after)) {
    const a = after[id]; const b = during[id];
    expect(!!a.pitch).toBe(!!b.pitch);
    expect(!!a.timing).toBe(!!b.timing);
    if (a.pitch) { expect(a.pitch.manual).toBe(b.pitch.manual); expect(a.pitch.degree).toBeCloseTo(b.pitch.degree, 1); }
    if (a.timing) { expect(a.timing.manual).toBe(b.timing.manual); expect(a.timing.degree).toBeCloseTo(b.timing.degree, 1); }
  }
  await win.keyboard.press('Escape');
  await win.evaluate(() => { window.__app.render(); });
  const d = await drawn();
  const reds = d.lines.filter((c) => c !== TAKE_C && c !== WHITE);
  expect(reds.length).toBeGreaterThan(0);
  fs.mkdirSync(path.join(APP, 'screenshots'), { recursive: true });
  fs.mkdirSync(SHOTS, { recursive: true });
  const screenshot = await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-corr-colors.png') });
  fs.writeFileSync(path.join(SHOTS, 'issue45-auto-overlap.png'), screenshot);
});

test('(6) 取り消すと前の色（手動の白）に戻る', async () => {
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await settle();
  const c = byId(await corr());
  expect(c[edgeNote].band).toBe(WHITE);
});

test('実行時のコンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});
