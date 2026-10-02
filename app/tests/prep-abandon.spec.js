// 取り消しを出さない準備の待ちから抜けられることを検証する（#63）。
//
//   (A1) ガイドの指定で準備に合流して待たされ、長くなったら（テストでは 1.5 秒）ポップアップに「待つのをやめる（Esc）」
//   (A2) Esc でやめると、そのトラックの下を空にして「準備中。終わったら表示する」。編集（Ctrl+Z）は「準備中」で断る
//   (A3) 裏の準備が終わったら（見出しの印のポーリング）、自動で描き直す（ガイドも重なる）
//
// 準備を遅らせるのはエンジンのテスト用の環境変数（`VOCAL_ENGINE_PREP_TEST_DELAY`・`VOCAL_ENGINE_PREP_TEST_MATCH`）。
// 「待つのをやめる」を出すまでの時間は、画面のテスト用の setAbandonAfter（既定 15 秒）で縮める。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'projects', '_test-prep-abandon');
const MEDIA = path.join(ROOT, 'Media');
const TAKE = path.join(MEDIA, 'take.wav');
const SLOW = path.join(MEDIA, 'slowprep_guide.wav');
const PROJECT = path.join(ROOT, 'proj');
const USERDATA = path.join(ROOT, 'userdata');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  env.VOCAL_ENGINE_PROJECTS = path.join(ROOT, 'projects');
  env.VOCAL_ENGINE_WORK_DIR = path.join(ROOT, 'work');
  env.VOCAL_ENGINE_PREP_TEST_DELAY = '1.0';
  env.VOCAL_ENGINE_PREP_TEST_MATCH = 'slowprep_';
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(MEDIA, { recursive: true });
  fs.copyFileSync(M.clip('C'), TAKE);
  fs.copyFileSync(M.clip('C2'), SLOW);
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
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening && !window.__app.S.openQueued,
    null, { timeout: 240000 });
}
const status = () => win.evaluate(() => window.__app.status());
const current = () => win.evaluate(() => window.__app.S.session?.current);

let takeId;
let slowId;

test('(A1〜A3) 取り消しを出さない待ちでも「待つのをやめる（Esc）」で抜け、準備が終わったら描き直す', async () => {
  test.setTimeout(240000);
  await settle();
  takeId = await current();
  // 遅いトラックを足して選び、一度準備を済ませる（ここは取り消しを出す待ち）
  slowId = (await win.evaluate((p) => window.__app.addTrackFile(p), SLOW)).id;
  await settle();
  expect(await win.evaluate((id) => window.__app.selectTrack(id), slowId)).toBe(true);
  await settle();
  expect(await current()).toBe(slowId);
  const notes = await win.evaluate(() => window.__app.notes().length);
  expect(notes).toBeGreaterThan(0);

  // ガイドを指定: 遅いトラックの準備（新しい組み合わせ）に合流して待つ。取り消しは出さない
  await win.evaluate(async () => {
    const { setAbandonAfter } = await import('./engine.js');
    setAbandonAfter(1500);
  });
  await win.evaluate((id) => { window.guiding = window.__app.setGuide(id); }, takeId);
  await expect(win.locator('#busyCenter')).toBeVisible({ timeout: 10000 });
  await expect(win.locator('#busyLabel')).toHaveText('ガイドと対応付けている');
  expect((await win.evaluate(() => window.__app.busy())).popup.cancel).toBe(false);
  // (A1) 長くなったら抜ける道を出す
  await expect(win.locator('#busyCancel')).toBeVisible({ timeout: 10000 });
  await expect(win.locator('#busyCancel')).toHaveText('待つのをやめる（Esc）');
  await win.screenshot({ path: path.join(APP, 'test-results', 'prep-abandon.png') });

  // (A2) Esc でやめる: 下を空にし、準備中と伝える
  await win.keyboard.press('Escape');
  await win.evaluate(() => window.guiding);
  await expect(win.locator('#busyScrim')).toBeHidden({ timeout: 3000 });
  expect(await status()).toBe('準備中。終わったら表示する（トラックを選ぶと、もう一度待つ）');
  expect(await current()).toBe(slowId);
  expect(await win.evaluate(() => window.__app.notes().length)).toBe(0);
  expect(await win.evaluate(() => window.__app.S.awaitPrep?.id)).toBe(slowId);
  expect(['preparing', 'queued']).toContain(
    (await win.evaluate(() => window.__app.tracksState().heads)).find((h) => h.id === slowId).prep);
  // 編集はエンジンの「準備中」と同じく断る（エンジンに頼んで待たせない）
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await expect.poll(status, { timeout: 5000 }).toBe('準備中。少し待ってからもう一度');
  expect(await win.evaluate(() => window.__app.notes().length)).toBe(0);

  // (A3) 準備が終わったら、自動で描き直す（ガイドも重なる）
  await expect.poll(status, { timeout: 120000 }).toContain('の準備ができたので表示した');
  await settle();
  expect(await win.evaluate(() => window.__app.notes().length)).toBeGreaterThan(0);
  expect(await win.evaluate(() => window.__app.S.awaitPrep)).toBeNull();
  expect(await win.evaluate(() => !!window.__app.S.guide)).toBe(true);
  expect(await current()).toBe(slowId);
  await win.evaluate(async () => {
    const { setAbandonAfter } = await import('./engine.js');
    setAbandonAfter(15000);
  });
  expect(errors).toEqual([]);
});
