// トラックの並び順（issue #38）・トラックビューの表示範囲（issue #39）・再生位置に追従とスナップのアイコン（issue #40）。
//
//   (O1) 見出しを上下にドラッグ → 並び順。ドラッグ中から並びが入れ替わって見える（= 離した後）。取り消し・やり直し
//   (O2) 動かさずに押して離すのは今までどおり名前のクリック（編集対象の切り替え）
//   (T1) トラックビュー: 横ズーム（Ctrl+Shift+ホイール）・横スクロール（Shift+ホイール）は上だけ。白枠は残る
//   (T2) エディターのホイールは上の表示を変えない。表示 > ズームを戻すで全体表示。前回の表示として残す
//   (F1) 再生位置に追従: ヘッダーのボタン・F・メニューバー・設定画面。オフなら画面を送らない
//   (F2) 時間スナップのアイコン（縦線から右へ横線）・追従のアイコン（右向きの矢印が縦線にぶつかる）
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2', 'E');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'projects', '_test-track-order-view');
const MEDIA = path.join(ROOT, 'Media');
const TAKE = path.join(MEDIA, 'take.wav');
const GUIDE = path.join(MEDIA, 'guide.wav');
const INST = path.join(MEDIA, 'Inst_mix.wav');
const PROJECT = path.join(ROOT, 'proj');
const USERDATA = path.join(ROOT, 'userdata');
const DOCS = path.join(APP, 'screenshots');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  env.VOCAL_ENGINE_PROJECTS = path.join(ROOT, 'projects');
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(MEDIA, { recursive: true });
  fs.copyFileSync(M.clip('C'), TAKE);
  fs.copyFileSync(M.clip('C2'), GUIDE);
  fs.copyFileSync(M.clip('E'), INST);
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--guide', GUIDE, '--project-dir', PROJECT,
      '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await settle();
  await win.evaluate(async (p) => { await window.__app.addTrackFile(p); }, INST);
  await settle();
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 240000 });
}
const tstate = () => win.evaluate(() => window.__app.tracksState());
const ids = async () => (await win.evaluate(() => window.__app.tracks())).map((t) => t.name);
const domHeads = () => win.evaluate(() => [...document.querySelectorAll('#heads .th .nm')].map((e) => e.textContent));
const hist = () => win.evaluate(() => window.__app.hist());
const settings = () => JSON.parse(fs.readFileSync(path.join(USERDATA, 'state.json'), 'utf8'));
// タイトルバーのメニューバー（main の Menu を写したものを titlebar.js が描いている）
const menuBar = () => win.evaluate(() => window.__app.menubar().model.map((m) => ({
  label: m.label,
  items: (m.submenu || []).filter((x) => x.type !== 'separator')
    .map((x) => ({ label: x.label, accel: x.accelerator || null, checked: x.checked })),
})));

async function wheelAt(x, y, dx, dy, mods = []) {
  await win.mouse.move(x, y);
  for (const k of mods) await win.keyboard.down(k);
  await win.mouse.wheel(dx, dy);
  for (const k of mods.slice().reverse()) await win.keyboard.up(k);
}

// ---------------------------------------------------------------- #38
test('(O1) 見出しを上下にドラッグして並び替え。ドラッグ中 = 離した後。取り消せる', async () => {
  expect(await ids()).toEqual(['take', 'guide', 'Inst_mix']);
  const cur0 = await win.evaluate(() => window.__app.S.session.current);
  const th = await tstate();
  const inst = await win.locator('#heads .th').nth(2).locator('.nm').boundingBox();
  const first = await win.locator('#heads .th').nth(0).boundingBox();
  await win.mouse.move(inst.x + 10, inst.y + inst.height / 2);
  await win.mouse.down();
  await win.mouse.move(inst.x + 10, first.y + 6, { steps: 8 });
  // ドラッグ中から並びが入れ替わって見える（見出しもレーンも）
  expect(await domHeads()).toEqual(['Inst_mix', 'take', 'guide']);
  const mid = await tstate();
  expect(mid.clips.map((c) => c.row)).toEqual([0, 1, 2]);
  expect(mid.order[0]).toBe(th.order[2]);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-tracks-order.png') });
  await win.mouse.up();
  await settle();
  expect(await ids()).toEqual(['Inst_mix', 'take', 'guide']);
  expect(await domHeads()).toEqual(['Inst_mix', 'take', 'guide']);
  expect((await tstate()).pendingOrder).toBeNull();
  expect(await win.evaluate(() => window.__app.S.session.current)).toBe(cur0);   // クリックにはならない
  expect((await hist()).undo.label).toBe('トラックの順番');
  // 白枠（下の表示範囲）は編集中のトラックの行（2 行目）に付いてくる
  expect((await tstate()).frame.row).toBe(1);
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await settle();
  expect(await domHeads()).toEqual(['take', 'guide', 'Inst_mix']);
  await win.keyboard.press('Control+Shift+z');
  await settle();
  expect(await domHeads()).toEqual(['Inst_mix', 'take', 'guide']);
  // 下へ: 伴奏を一番下に戻す
  const top = await win.locator('#heads .th').nth(0).locator('.nm').boundingBox();
  const last = await win.locator('#heads .th').nth(2).boundingBox();
  await win.mouse.move(top.x + 10, top.y + top.height / 2);
  await win.mouse.down();
  await win.mouse.move(top.x + 10, last.y + last.height - 4, { steps: 8 });
  await win.mouse.up();
  await settle();
  expect(await ids()).toEqual(['take', 'guide', 'Inst_mix']);
});

test('(O2) 動かさずに離すのは名前のクリック（編集対象の切り替え）のまま。M・S のボタンも効く', async () => {
  await win.locator('#heads .th').nth(1).locator('.nm').click();
  await win.waitForFunction((p) => window.__app.S.take?.path === p, GUIDE, { timeout: 120000 });
  await settle();
  await win.locator('#heads .th').nth(0).locator('.nm').click();
  await win.waitForFunction((p) => window.__app.S.take?.path === p, TAKE, { timeout: 120000 });
  await settle();
  await win.locator('#heads .th').nth(2).locator('button[data-act="m"]').click();
  await expect.poll(async () => (await win.evaluate(() => window.__app.tracks()))[2].mute).toBe(true);
  await win.locator('#heads .th').nth(2).locator('button[data-act="m"]').click();
  await expect.poll(async () => (await win.evaluate(() => window.__app.tracks()))[2].mute).toBe(false);
  expect(await ids()).toEqual(['take', 'guide', 'Inst_mix']);
});

// ---------------------------------------------------------------- #39
test('(T1) トラックビューの横ズーム・横スクロールは上だけ（ポインタの下の時刻は動かない）。白枠は残る', async () => {
  await win.evaluate(() => { window.__app.setViewTimeline(1.0, 2.0); window.__app.render(); });
  const lanes = await win.locator('#lanes').boundingBox();
  const ed0 = await win.evaluate(() => ({ ...window.__app.S.view }));
  const st0 = await tstate();
  expect(st0.view).toBeNull();                               // 既定は全体表示
  const x = lanes.x + lanes.width * 0.4;
  const tAt = (px) => win.evaluate((v) => window.__app.tvT(v), px - lanes.x);
  const t0 = await tAt(x);
  for (let i = 0; i < 6; i++) await wheelAt(x, lanes.y + 10, 0, -100, ['Control', 'Shift']);
  await expect.poll(async () => (await tstate()).view?.span ?? 99).toBeLessThan((st0.range[1] - st0.range[0]) * 0.6);
  expect(await tAt(x)).toBeCloseTo(t0, 2);
  expect(await win.evaluate(() => ({ ...window.__app.S.view }))).toEqual(ed0);   // 下は変わらない
  const st1 = await tstate();
  const pps = st1.laneW / (st1.range[1] - st1.range[0]);
  expect(st1.frame.x).toBeCloseTo((1.0 - st1.range[0]) * pps, 0);            // 白枠は上の表示に合わせて描く
  expect(st1.frame.w).toBeCloseTo(1.0 * pps, 0);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-tracks-zoom.png') });
  // Shift+ホイール: 横スクロール（幅はそのまま）
  await wheelAt(x, lanes.y + 10, 0, 100, ['Shift']);
  await expect.poll(async () => (await tstate()).view.t0).toBeGreaterThan(st1.view.t0);
  expect((await tstate()).view.span).toBeCloseTo(st1.view.span, 6);
  // Ctrl+ホイール: トラックの高さ（今までどおり）
  const h0 = (await tstate()).trackH;
  await wheelAt(x, lanes.y + 10, 0, -100, ['Control']);
  await expect.poll(async () => (await tstate()).trackH).toBeGreaterThan(h0);
  await wheelAt(x, lanes.y + 10, 0, 100, ['Control']);
  await expect.poll(async () => (await tstate()).trackH).toBe(h0);
  expect(await win.evaluate(() => ({ ...window.__app.S.view }))).toEqual(ed0);
});

test('(T2) エディターのホイールは上を変えない。ズームを戻すで全体表示。前回の表示として残す', async () => {
  const v1 = (await tstate()).view;
  const r = await win.locator('#roll').boundingBox();
  await wheelAt(r.x + 600, r.y + 200, 0, -100, ['Control', 'Shift']);
  await wheelAt(r.x + 600, r.y + 200, 0, 100, ['Shift']);
  expect((await tstate()).view).toEqual(v1);
  await expect.poll(() => settings().view?.tv?.span ?? null).toBeCloseTo(v1.span, 6);
  // 縮めすぎた分は全体表示に戻る（全体より広くはならない）
  const lanes = await win.locator('#lanes').boundingBox();
  for (let i = 0; i < 30; i++) await wheelAt(lanes.x + 200, lanes.y + 10, 0, 100, ['Control', 'Shift']);
  await expect.poll(async () => (await tstate()).view).toBeNull();
  for (let i = 0; i < 4; i++) await wheelAt(lanes.x + 200, lanes.y + 10, 0, -100, ['Control', 'Shift']);
  await expect.poll(async () => (await tstate()).view).not.toBeNull();
  await win.evaluate(() => window.__app.onMenu({ cmd: 'zoom-reset' }));
  expect((await tstate()).view).toBeNull();
});

// ---------------------------------------------------------------- #40
test('(F1) 再生位置に追従: ボタン・F・メニューバー・設定画面。オフなら画面を送らない', async () => {
  const b = win.locator('#bFollow');
  await expect(b).toHaveAttribute('aria-pressed', 'true');                 // 既定はオン（前の版と同じ動き）
  expect(await b.getAttribute('title')).toBe('再生位置に追従（F）');
  await win.locator('#mock').focus();
  await win.keyboard.press('f');
  await expect(b).toHaveAttribute('aria-pressed', 'false');
  expect(settings().grid.follow).toBe(false);
  await expect.poll(async () => (await menuBar()).find((m) => m.label === '表示').items
    .find((x) => x.label === '再生位置に追従')).toMatchObject({ accel: 'F', checked: false });
  // オフ: 再生位置が表示範囲を出ても送らない（上をズームしていても）
  const lanes = await win.locator('#lanes').boundingBox();
  for (let i = 0; i < 6; i++) await wheelAt(lanes.x + 20, lanes.y + 10, 0, -100, ['Control', 'Shift']);
  await expect.poll(async () => (await tstate()).view).not.toBeNull();
  await win.evaluate(() => { window.__app.S.view = { t0: 0.2, span: 0.4 }; window.__app.S.head = 0.45; window.__app.render(); });
  const tv0 = (await tstate()).view;
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 120000 });
  await win.waitForFunction((t) => window.__app.S.head > t, tv0.t0 + tv0.span + 0.1, { timeout: 30000 });
  expect(await win.evaluate(() => window.__app.S.view.t0)).toBeCloseTo(0.2, 6);
  expect((await tstate()).view).toEqual(tv0);
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => !window.__app.S.playing, null, { timeout: 10000 });
  // オン（ボタン）: 送る。上もズームしていれば送る
  await b.click();
  await expect(b).toHaveAttribute('aria-pressed', 'true');
  expect(settings().grid.follow).toBe(true);
  await win.evaluate(() => { window.__app.S.view = { t0: 0.2, span: 0.4 }; window.__app.S.head = 0.45; window.__app.render(); });
  await win.evaluate((v) => window.__app.setTracksView(v), tv0);
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 120000 });
  await win.waitForFunction(() => window.__app.S.view.t0 > 0.3, null, { timeout: 30000 });
  await win.waitForFunction((t) => (window.__app.tracksState().view?.t0 ?? 0) > t, tv0.t0 + 0.01, { timeout: 30000 });
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => !window.__app.S.playing, null, { timeout: 10000 });
  await win.evaluate(() => window.__app.onMenu({ cmd: 'zoom-reset' }));
  // 設定画面にもある（表示のグループ・F）
  await win.keyboard.press('Control+,');
  await expect(win.locator('#keys')).toBeVisible();
  await expect(win.locator('#keys .kr[data-id="follow"] .n')).toHaveText('再生位置に追従');
  await expect(win.locator('#keys .kr[data-id="follow"] .k')).toHaveText('F');
  await win.keyboard.press('Escape');
  await expect(win.locator('#keys')).toBeHidden();
});

test('(F2) アイコン: 追従は右向きの矢印が縦線にぶつかる形、時間スナップは縦線から右へ横線', async () => {
  const d = (sel) => win.evaluate((s) => [...document.querySelectorAll(`${s} svg path`)].map((p) => p.getAttribute('d')), sel);
  expect(await d('#bFollow')).toEqual(['M4 12h12', 'm12 8 4 4-4 4', 'M19.5 5v14']);
  expect(await d('#bSnapT')).toEqual(['M7 5v14', 'M7 12h12']);
  // 色はほかのヘッダーのアイコンと同じ（線の色 = 文字の色）
  const col = (sel) => win.evaluate((s) => getComputedStyle(document.querySelector(`${s} svg`)).stroke, sel);
  expect(await col('#bSnapT')).toBe(await col('#bSnapP'));
  await win.locator('#mock').focus();
  await win.locator('#bFollow').hover();
  await win.screenshot({ path: path.join(DOCS, 'screenshot-follow.png'), clip: { x: 0, y: 0, width: 760, height: 36 } });
});

test('エラーが出ていない', async () => {
  expect(errors).toEqual([]);
});
