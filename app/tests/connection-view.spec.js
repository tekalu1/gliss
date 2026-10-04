// 接続の見せ方（issue #2 の B 案「触れた境目だけノードとエッジ」）。
//
//   (1) 普段は何も出さない
//   (2) 境目に近づくと、その境目だけに点と線（接続 = 塗りの点＋実線）
//   (3) Alt を押した瞬間、線が破線・点が白抜き（ここで切れる予告）。離すと戻る
//   (4) ノートを選ぶと、その両側の境目に出る
//   (5) Alt+ドラッグ: ドラッグ中は切り離し（白抜きの点）。曲線はドラッグ中 = 離した後
//       （ピッチを動かしたノートの境目。つなぎが離した後に消えることは無い）
//   (6) 切り離した端を隣まで伸ばして吸着: ドラッグ中に実線でつながる。曲線もドラッグ中 = 離した後
//   (7) ピッチを動かすと、段差なら縦線、なだらかなら S 字（線の曲がる幅 = なだらかさの窓）
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
const PROJECT = path.join(REPO, 'projects', '_test-connection-view');
const USERDATA = `${PROJECT}-userdata`;
const DOCS = path.join(APP, 'screenshots');
const PAIR = 'n005|n006';

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ...M.RMVPE_ENV, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
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
  await win.evaluate(() => { window.__app.S.view = { t0: 0.3, span: 1.4 }; window.__app.render(); });
  fs.mkdirSync(DOCS, { recursive: true });
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
}
const glyphs = () => win.evaluate(() => window.__app.glyphs());
const glyph = async (key) => (await glyphs()).find((g) => g.key === key) || null;
const conns = async () => new Map((await win.evaluate(() => window.__app.connections()))
  .map((c) => [c.id, c]));
const edge = (id, which) => win.locator(`#roll rect[data-note="${id}"][data-edge="${which}"]`).first();
const blob = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();

/** 境目（n005 の尻）の画面上の位置。 */
async function boundaryXY() {
  const e = await edge('n005', 'end').boundingBox();
  return { x: e.x + e.width, y: e.y + e.height / 2 };
}

/** 2 本の曲線（フレームごとの MIDI）の差の最大（セント）。 */
function maxDiffCents(a, b) {
  let m = 0;
  for (let i = 0; i < a.length; i++) {
    if (a[i] == null || b[i] == null) continue;
    m = Math.max(m, Math.abs(a[i] - b[i]) * 100);
  }
  return m;
}

/** 線の path（M 左の足 L … L 右の足）から、曲がる部分の横幅（px）を取る。足は左右 8 px。 */
function bendWidth(d) {
  const xs = [...d.matchAll(/[ML](-?[\d.]+) (-?[\d.]+)/g)].map((m) => +m[1]);
  return (xs[xs.length - 1] - 8) - (xs[0] + 8);
}

/** 端をつまんで dx px 動かし、離す直前に mid() を呼んでから離す。 */
async function dragEdge(id, which, dx, { alt = false, mid = null } = {}) {
  const box = await edge(id, which).boundingBox();
  const x = box.x + box.width / 2; const y = box.y + box.height / 2;
  await win.mouse.move(x, y);
  if (alt) await win.keyboard.down('Alt');
  await win.mouse.down();
  await win.mouse.move(x + dx, y, { steps: 10 });
  await win.waitForFunction(() => window.__app.plan() !== null, null, { timeout: 30000 });
  await win.mouse.move(x + dx + 0.01, y);
  const out = mid ? await mid() : null;
  await win.mouse.up();
  if (alt) await win.keyboard.up('Alt');
  await settle();
  return out;
}

test('(1) 普段は何も出さない', async () => {
  await win.mouse.move(5, 5);
  expect(await glyphs()).toEqual([]);
  expect((await conns()).get('n005').next).toBe(true);
});

test('(2) 境目に近づくと、その境目だけに塗りの点＋実線', async () => {
  const b = await boundaryXY();
  await win.mouse.move(b.x - 18, b.y);
  const gs = await glyphs();
  expect(gs.map((g) => g.key)).toEqual([PAIR]);
  expect(gs[0].state).toBe('connected');
  expect(gs[0].dashed).toBe(false);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-conn-near.png') });
  // 境目から離れると消える
  await win.mouse.move(b.x - 200, b.y - 150);
  expect(await glyphs()).toEqual([]);
});

test('(3) Alt を押した瞬間に破線（切れる予告）、離すと戻る', async () => {
  const b = await boundaryXY();
  await win.mouse.move(b.x - 18, b.y);
  await win.locator('#mock').focus();
  await win.keyboard.down('Alt');
  const g = await glyph(PAIR);
  expect(g.state).toBe('cut');
  expect(g.dashed).toBe(true);
  // 接続された端のカーソルも「自分だけ動く」に変わる
  const cur = await win.evaluate(() => document.querySelector('#roll rect[data-note="n005"][data-edge="end"]').style.cursor);
  expect(cur).toBe('ew-resize');
  await win.screenshot({ path: path.join(DOCS, 'screenshot-conn-alt.png') });
  await win.keyboard.up('Alt');
  const g2 = await glyph(PAIR);
  expect(g2.state).toBe('connected');
  expect(g2.dashed).toBe(false);
  await win.mouse.move(5, 5);
});

test('(4) ノートを選ぶと、その両側の境目に出る', async () => {
  await blob('n006').click();
  const keys = (await glyphs()).map((g) => g.key).sort();
  expect(keys).toEqual(['n005|n006', 'n006|n007']);
  await win.locator('#mock').focus();
  await win.keyboard.press('Escape');
  await win.mouse.move(5, 5);
  expect(await glyphs()).toEqual([]);
});

test('(5) Alt+ドラッグ: ドラッグ中から切り離しで描き、曲線はドラッグ中 = 離した後', async () => {
  // 後ろのノートを 2 半音上げておく（つなぎが曲線に出る。切るとつなぎが消える）
  await win.evaluate(async () => {
    await window.api.call('shift_pitch', { cents: 200, note_id: 'n006', author: 'human' });
    await window.__app.refresh();
  });
  await settle();
  expect((await win.evaluate(() => window.__app.transitions()))
    .some((t) => `${t.a}|${t.b}` === PAIR && Math.abs(t.delta) > 100)).toBe(true);
  const before = await win.evaluate(() => window.__app.engineCurve());
  const mid = await dragEdge('n005', 'end', -40, {
    alt: true,
    mid: async () => {
      await win.screenshot({ path: path.join(DOCS, 'screenshot-conn-altdrag.png') });
      return { g: await glyph(PAIR), curve: await win.evaluate(() => window.__app.editedCurve()) };
    },
  });
  const after = await win.evaluate(() => window.__app.engineCurve());
  expect(maxDiffCents(mid.curve, after)).toBeLessThan(1);
  expect(maxDiffCents(before, mid.curve)).toBeGreaterThan(20);  // なだらかさが消えるのがドラッグ中から見える（issue #6）
  expect(mid.g.state).toBe('detached');
  expect((await conns()).get('n005').next).toBe(false);
});

test('(6) 隣まで伸ばして吸着: ドラッグ中に実線でつながり、曲線もドラッグ中 = 離した後', async () => {
  const gapPx = await win.evaluate(() => {
    const n5 = window.__app.shapes().find((n) => n.id === 'n005');
    const n6 = window.__app.shapes().find((n) => n.id === 'n006');
    const r = document.querySelector('#roll').getBoundingClientRect();
    return (n6.s - n5.e) / window.__app.S.view.span * (r.width - 44);
  });
  const before = await win.evaluate(() => window.__app.engineCurve());
  const mid = await dragEdge('n005', 'end', gapPx + 30, {
    mid: async () => {
      await win.screenshot({ path: path.join(DOCS, 'screenshot-conn-snap.png') });
      return { g: await glyph(PAIR), curve: await win.evaluate(() => window.__app.editedCurve()),
        plan: await win.evaluate(() => window.__app.plan()) };
    },
  });
  expect(mid.plan.snap).not.toBeNull();
  const after = await win.evaluate(() => window.__app.engineCurve());
  expect(maxDiffCents(mid.curve, after)).toBeLessThan(1);
  expect(mid.g.state).toBe('connected');
  expect(mid.g.dashed).toBe(false);
  expect((await conns()).get('n005').next).toBe(true);
  expect(maxDiffCents(before, after)).toBeGreaterThan(20);   // つなぎが戻った（プレビューにも出ていた）
});

test('(7) ピッチを動かす: 段差なら縦線、なだらかなら S 字', async () => {
  // ピッチのドラッグ中は、動かしているノートの両側の境目に出る
  const box = await blob('n006').boundingBox();
  const x = box.x + box.width / 2; const y = box.y + box.height / 2;
  await win.mouse.move(x, y);
  await win.mouse.down();
  await win.mouse.move(x, y - 25, { steps: 8 });
  const keys = (await glyphs()).map((g) => g.key).sort();
  expect(keys).toEqual(['n005|n006', 'n006|n007']);
  const auto = await glyph(PAIR);
  expect(bendWidth(auto.path)).toBeGreaterThan(2);                 // 自動 = なだらか（S 字）
  await win.screenshot({ path: path.join(DOCS, 'screenshot-conn-pitch.png') });
  await win.mouse.up();
  await settle();
  // なだらかさ 0（段差）: 線は縦になる
  await win.evaluate(async () => {
    await window.api.call('set_transition', { value: 0, note_a: 'n005', note_b: 'n006', author: 'human' });
    await window.__app.refresh();
  });
  await settle();
  await blob('n006').click();
  const step = await glyph(PAIR);
  expect(Math.abs(bendWidth(step.path))).toBeLessThan(0.2);
  await win.locator('#mock').focus();
  await win.keyboard.press('Escape');
});

test('(8) Alt で掴んで 1 px しか動かさない: 離しても何も起きないので、ドラッグ中も「切れる予告」のまま', async () => {
  await win.mouse.move(5, 5);
  const mid = await dragEdge('n005', 'end', -1, { alt: true, mid: () => glyph(PAIR) });
  expect(mid.state).toBe('cut');                        // 「切り離し」にちらつかない
  expect((await conns()).get('n005').next).toBe(true);
});

test('(9) 選択ノートが画面の外: 記号が無いので Alt はふつうの Alt（メニューバーを止めない）', async () => {
  await blob('n006').click();
  expect(await win.evaluate(() => window.__app.hasConnFocus())).toBe(true);
  await win.evaluate(() => { window.__app.S.view = { t0: 2.4, span: 1.2 }; window.__app.render(); });
  expect(await glyphs()).toEqual([]);
  expect(await win.evaluate(() => window.__app.hasConnFocus())).toBe(false);
  await win.locator('#mock').focus();
  await win.keyboard.press('Escape');
  await win.evaluate(() => { window.__app.S.view = { t0: 0.3, span: 1.4 }; window.__app.render(); });
});

test('(10) 境目に近づいたままホイールで動かすと、ポインタの下に無くなった境目の記号は消える', async () => {
  const b = await boundaryXY();
  await win.mouse.move(b.x - 18, b.y);
  expect((await glyphs()).map((g) => g.key)).toEqual([PAIR]);
  await win.keyboard.down('Shift');                      // 横へ送る（ポインタは動かさない。Shift+ホイール = 横スクロール。#27）
  await win.mouse.wheel(0, 600);
  await win.keyboard.up('Shift');
  const gs = await glyphs();
  const now = await win.evaluate(([x, y]) => window.__app.nearPair(x, y), [b.x - 18, b.y]);
  expect(gs.map((g) => g.key)).toEqual(now ? [now] : []);
  await win.evaluate(() => { window.__app.S.view = { t0: 0.3, span: 1.4 }; window.__app.render(); });
  await win.mouse.move(5, 5);
});

test('実行時のコンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});
