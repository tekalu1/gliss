// 裏の準備の表示（#63）: 見出しの「準備中」の印と、準備が終わる前に開いたときのポップアップ。
//
//   (1) 足したトラックの見出しに準備中の印（段階名はツールチップ）。準備が済むと消える
//   (2) 準備が終わる前に開くと、覆い付きのポップアップで待たせる。後ろのクリックは効かない。Esc で前のトラックに戻る
//       （合流を取り消しても、裏の準備は続く）
//   (3) 準備済みのトラックへの切り替えでは、ポップアップも覆いも出さない
//
// 準備を遅らせるのはエンジンのテスト用の環境変数（`VOCAL_ENGINE_PREP_TEST_DELAY`: 段の頭ごとに待つ秒、
// `VOCAL_ENGINE_PREP_TEST_MATCH`: そのトラックだけ。engine/vocal_engine/prep.py の _test_delay）。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'B');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'projects', '_test-prep-busy');
const MEDIA = path.join(ROOT, 'Media');
const TAKE = path.join(MEDIA, 'take.wav');
const SLOW = path.join(MEDIA, 'slowprep_take2.wav');
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
  env.VOCAL_ENGINE_PREP_TEST_DELAY = '1.2';
  env.VOCAL_ENGINE_PREP_TEST_MATCH = 'slowprep_';
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(MEDIA, { recursive: true });
  fs.copyFileSync(M.clip('C'), TAKE);
  fs.copyFileSync(M.clip('B'), SLOW);
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
const heads = () => win.evaluate(() => window.__app.tracksState().heads);
const headOf = async (id) => (await heads()).find((h) => h.id === id);
const current = () => win.evaluate(() => window.__app.S.session?.current);

/** ページの中で、覆い・ポップアップを出したかを記録し始める（hidden の変化を MutationObserver で。
 * テストのウィンドウは透明で、requestAnimationFrame は間引かれることがあるので使わない）。 */
async function watch() {
  await win.evaluate(() => {
    window.seen = { scrim: 0, pop: 0 };
    window.seenObs = new MutationObserver((recs) => {
      for (const r of recs) {
        if (r.target.hidden) continue;
        if (r.target.id === 'busyScrim') window.seen.scrim += 1;
        if (r.target.id === 'busyCenter') window.seen.pop += 1;
      }
    });
    for (const id of ['#busyScrim', '#busyCenter']) {
      window.seenObs.observe(document.querySelector(id), { attributes: true, attributeFilter: ['hidden'] });
    }
  });
}
const seen = () => win.evaluate(() => { window.seenObs.disconnect(); return window.seen; });

let takeId;
let slowId;

test('(1) 足したトラックの見出しに準備中の印を出し、段階名をツールチップに出す', async () => {
  await settle();
  takeId = await current();
  const t = await win.evaluate((p) => window.__app.addTrackFile(p), SLOW);
  slowId = t.id;
  await settle();
  expect(await current()).toBe(takeId);            // 足すだけ（編集対象は変えない）
  await expect.poll(async () => (await headOf(slowId))?.prep, { timeout: 30000 }).toBe('preparing');
  await expect.poll(async () => (await headOf(slowId))?.prepTip || '', { timeout: 30000 })
    .toMatch(/^準備中: .+ \d+%/);
  expect((await headOf(takeId)).prep).toBeNull();  // 準備済みのトラックには出さない
  const tv = win.locator('#tv');
  await tv.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-prep-mark.png') });
  await win.screenshot({ path: path.join(APP, 'test-results', 'prep-mark.png') });
});

test('(2) 準備が終わる前に開くとポップアップで待たせ、後ろのクリックは効かず、Esc で前のトラックに戻る', async () => {
  const before = await win.evaluate(() => ({ view: window.__app.view(), notes: window.__app.notes().length }));
  const name = (await win.evaluate(() => window.__app.tracks())).find((t) => t.id === slowId).name;
  await win.evaluate((id) => { window.switching = window.__app.selectTrack(id); }, slowId);
  await expect(win.locator('#busyCenter')).toBeVisible({ timeout: 10000 });
  await expect(win.locator('#busyLabel')).toHaveText(`${name} を準備している`);
  await expect(win.locator('#busyTarget')).toHaveText(path.basename(SLOW));
  await expect(win.locator('#busyNote')).toHaveText('終わるまで編集できません');
  await expect(win.locator('#busyCancel')).toBeVisible();
  // 段階名（エンジンの stage_label）と進み具合
  await expect(win.locator('#busyStage')).toHaveText(/^.+ · \d+ 秒$/, { timeout: 10000 });
  await expect(win.locator('#busyPercent')).toHaveText(/^\d+%$/, { timeout: 10000 });
  expect(await win.evaluate(() => document.activeElement?.id)).toBe('busyCenter');
  await win.waitForTimeout(400);                   // 覆いが暗くなり、進み具合の線が伸びきってから撮る
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-busy.png') });
  await win.screenshot({ path: path.join(APP, 'test-results', 'busy-prep.png') });
  // 後ろ（ピアノロール・トラックの見出し）のクリックは効かない。ポップアップを揺らす
  const roll = await win.locator('#roll').boundingBox();
  await win.mouse.click(roll.x + roll.width * 0.3, roll.y + roll.height * 0.5);
  expect(await win.locator('#busyCenter').evaluate((el) => el.getAnimations().length)).toBeGreaterThan(0);
  await win.screenshot({ path: path.join(APP, 'test-results', 'busy-prep-click.png') });
  const head = await win.locator(`#heads .th[data-id="${takeId}"] .nm`).boundingBox();
  await win.mouse.click(head.x + 10, head.y + head.height / 2);   // 覆いが受けるので、座標でクリックする
  await win.keyboard.press('2');                   // ツールの切り替え（鉛筆）も届かない
  expect(await win.evaluate(() => window.__app.tool())).not.toBe('draw');
  // Esc: 前のトラックに戻る（表示範囲も戻す）
  await win.keyboard.press('Escape');
  expect(await win.evaluate(() => window.switching)).toBe(false);
  await settle();
  expect(await current()).toBe(takeId);
  expect(await win.evaluate(() => window.__app.status())).toContain('戻した');
  const after = await win.evaluate(() => ({ view: window.__app.view(), notes: window.__app.notes().length }));
  expect(after.notes).toBe(before.notes);
  expect(after.view.t0).toBeCloseTo(before.view.t0, 3);
  expect(after.view.span).toBeCloseTo(before.view.span, 3);
  await expect(win.locator('#busyScrim')).toBeHidden({ timeout: 2000 });
  // 合流を取り消しても、裏の準備は続く（印はまだ出ている）
  expect(['preparing', 'queued']).toContain((await headOf(slowId)).prep);
});

test('(1) 準備が済むと印が消え、(3) 準備済みのトラックへの切り替えでは何も出さない', async () => {
  await expect.poll(async () => (await headOf(slowId))?.prep ?? null, { timeout: 180000 }).toBeNull();
  await watch();
  expect(await win.evaluate((id) => window.__app.selectTrack(id), slowId)).toBe(true);
  await settle();
  expect(await current()).toBe(slowId);
  expect(await win.evaluate((id) => window.__app.selectTrack(id), takeId)).toBe(true);
  await settle();
  expect(await current()).toBe(takeId);
  expect(await seen()).toMatchObject({ scrim: 0, pop: 0 });
  expect(errors).toEqual([]);
});
