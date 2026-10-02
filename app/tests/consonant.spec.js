// 子音・息（音程の無いノート）の幅とタイミング（issue #35）。
//
//   (1) 矢印のときだけ、描いている帯（音のあるところ）とノートの端につかみがある。端に乗ると明るい縦線
//   (2) 端を横にドラッグ → 幅。ドラッグ中の見た目 = 離した後。接した隣が伸び縮み（接続）。Ctrl+Z で戻る
//   (3) 帯を横にドラッグ → 移動（長さはそのまま）。上下に動かしても音程は変わらない（横の移動だけ）
//   (4) Alt+ドラッグで切り離し（隣は動かない）。ドラッグ中は切り離しの記号
//   (5) 鉛筆・はさみのときはつかめない
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'A');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const TAKE = M.clip('A');          // 音程ノートに挟まれた無声（子音）がある
const PROJECT = path.join(REPO, 'projects', '_test-consonant');
const USERDATA = `${PROJECT}-userdata`;
const DOCS = path.join(APP, 'screenshots');

let app;
let win;
const errors = [];
let U = null;               // 動かす子音のノート { id, prev, next }

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  fs.rmSync(PROJECT, { recursive: true, force: true });
  fs.rmSync(USERDATA, { recursive: true, force: true });
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--project-dir', PROJECT, '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
}
const noPitch = () => win.evaluate(() => window.__app.noPitch());
const np = async (id) => (await noPitch()).find((n) => n.id === id);
const body = (id) => win.locator(`#roll rect[data-nop="${id}"]:not([data-nop-edge])`).first();
const edge = (id, which) => win.locator(`#roll rect[data-nop="${id}"][data-nop-edge="${which}"]`).first();
const shapes = () => win.evaluate(() => window.__app.shapes());
const hist = () => win.evaluate(() => window.__app.hist());

/** つかんで dx, dy px 動かし、離す直前の見かけ（mid）を返す。 */
async function drag(loc, dx, { dy = 0, alt = false, shot = null } = {}) {
  const b = await loc.boundingBox();
  const x = b.x + b.width / 2; const y = b.y + b.height / 2;
  await win.mouse.move(x, y);
  if (alt) await win.keyboard.down('Alt');
  await win.mouse.down();
  await win.mouse.move(x + dx, y + dy, { steps: 10 });
  await win.waitForFunction(() => window.__app.plan() !== null, null, { timeout: 30000 });
  await win.mouse.move(x + dx + 0.01, y + dy);
  if (shot) await win.screenshot({ path: path.join(DOCS, shot) });
  const mid = { nop: await noPitch(), shapes: await shapes(), plan: await win.evaluate(() => window.__app.plan()),
    glyphs: await win.evaluate(() => window.__app.glyphs()) };
  await win.mouse.up();
  if (alt) await win.keyboard.up('Alt');
  await settle();
  return mid;
}

test('(1) 子音の帯とノートの端につかみ。端に乗ると明るい縦線', async () => {
  // 前後が接した音程ノートの無声（子音）で、帯を描いているもの
  U = await win.evaluate(() => {
    const ns = window.__app.S.notes;
    for (let i = 1; i < ns.length - 1; i++) {
      const [a, u, b] = [ns[i - 1], ns[i], ns[i + 1]];
      if (u.kind === 'unvoiced' && a.kind === 'note' && b.kind === 'note'
        && Math.abs(u.start_sec - a.end_sec) < 1e-6 && Math.abs(b.start_sec - u.end_sec) < 1e-6) {
        return { id: u.id, prev: a.id, next: b.id, start: u.start_sec, end: u.end_sec };
      }
    }
    return null;
  });
  expect(U).not.toBeNull();
  await win.evaluate((u) => {
    window.__app.S.view = { t0: Math.max(0, u.start - 0.5), span: (u.end - u.start) + 1.0 };
    window.__app.render();
  }, U);
  expect((await np(U.id)).grab).toBe(true);
  await expect(body(U.id)).toHaveCount(1);
  await expect(edge(U.id, 'start')).toHaveCount(1);
  await expect(edge(U.id, 'end')).toHaveCount(1);
  const e = await edge(U.id, 'end').boundingBox();
  await win.mouse.move(e.x + e.width / 2, e.y + e.height / 2);
  await expect.poll(() => win.evaluate(() => window.__app.edgeHover())).toEqual({ id: U.id, which: 'end' });
  await expect(win.locator('#roll [data-edge-hot="end"]')).toHaveCount(1);
  // 接した隣と接続の見込み（カーソル）
  expect(await edge(U.id, 'end').evaluate((el) => el.style.cursor)).toBe('col-resize');
  await win.mouse.move(5, 5);
});

test('(2) 端のドラッグで幅。ドラッグ中の見た目 = 離した後、接した次のノートが縮む。Ctrl+Z で戻る', async () => {
  const s0 = await np(U.id);
  const next0 = (await shapes()).find((x) => x.id === U.next);
  const mid = await drag(edge(U.id, 'end'), 25, { shot: 'screenshot-consonant-drag.png' });
  const m = mid.nop.find((n) => n.id === U.id);
  expect(m.e - s0.e).toBeGreaterThan(0.01);                    // ドラッグ中から伸びて見える
  expect(Math.abs(m.s - s0.s)).toBeLessThan(1e-6);
  expect(mid.plan.kind).toBe('edge');
  const after = await np(U.id);
  expect(Math.abs(after.e - m.e)).toBeLessThan(0.002);         // 離した後 = ドラッグ中
  expect(Math.abs(after.s - m.s)).toBeLessThan(0.002);
  const next1 = (await shapes()).find((x) => x.id === U.next);
  expect(Math.abs(next1.s - after.e)).toBeLessThan(0.002);     // 次の頭は境目を共有（縮む）
  expect(Math.abs(next1.e - next0.e)).toBeLessThan(0.002);
  expect((await hist()).undo.label).toBe('ノートの長さ');
  await win.keyboard.press('Control+z');
  await settle();
  const back = await np(U.id);
  expect(Math.abs(back.e - s0.e)).toBeLessThan(1e-6);
});

test('(3) 帯のドラッグで移動（長さはそのまま）。上下に動かしても音程は変わらない', async () => {
  const s0 = await np(U.id);
  const prev0 = (await shapes()).find((x) => x.id === U.prev);
  // 縦に大きく動かしても横の移動だけ（子音は音程が無い）
  const mid = await drag(body(U.id), -18, { dy: -30 });
  const m = mid.nop.find((n) => n.id === U.id);
  expect(m.s - s0.s).toBeLessThan(-0.005);
  expect(Math.abs((m.e - m.s) - (s0.e - s0.s))).toBeLessThan(1e-4);
  const after = await np(U.id);
  expect(Math.abs(after.s - m.s)).toBeLessThan(0.002);
  expect(Math.abs((after.e - after.s) - (s0.e - s0.s))).toBeLessThan(0.002);
  const prev1 = (await shapes()).find((x) => x.id === U.prev);
  expect(Math.abs(prev1.e - after.s)).toBeLessThan(0.002);     // 前のノートの尻も一緒に（縮む）
  expect(prev1.pitch).toBeCloseTo(prev0.pitch, 6);
  const edits = await win.evaluate(() => window.__app.S.vd.edits || []);
  expect(edits.filter((e) => e.kind === 'pitch_shift')).toHaveLength(0);
  expect((await hist()).undo.label).toBe('ノートの移動');
  expect(await win.evaluate(() => window.__app.S.sel)).toEqual([U.id]); // 元の長さの表示用に選択する
  await win.keyboard.press('Control+z');
  await settle();
  expect(Math.abs((await np(U.id)).s - s0.s)).toBeLessThan(1e-6);
});

test('(4) Alt+ドラッグで切り離し（前のノートは動かない）。ドラッグ中は切り離しの記号', async () => {
  const s0 = await np(U.id);
  const prev0 = (await shapes()).find((x) => x.id === U.prev);
  const mid = await drag(edge(U.id, 'start'), 20, { alt: true });
  const g = mid.glyphs.find((x) => x.key === `${U.prev}|${U.id}`);
  expect(g?.state).toBe('detached');
  const after = await np(U.id);
  expect(after.s - s0.s).toBeGreaterThan(0.01);
  const prev1 = (await shapes()).find((x) => x.id === U.prev);
  expect(Math.abs(prev1.e - prev0.e)).toBeLessThan(1e-6);
  await win.keyboard.press('Control+z');
  await settle();
  expect(Math.abs((await np(U.id)).s - s0.s)).toBeLessThan(1e-6);
});

test('(5) 鉛筆・はさみのときはつかめない', async () => {
  await win.evaluate(() => window.__app.setTool('draw'));
  await expect(body(U.id)).toHaveCount(0);
  await win.evaluate(() => window.__app.setTool('cut'));
  await expect(body(U.id)).toHaveCount(0);
  await win.evaluate(() => window.__app.setTool('main'));
  await expect(body(U.id)).toHaveCount(1);
});

test('エラーが出ていない', async () => {
  expect(errors).toEqual([]);
});
