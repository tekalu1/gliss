// 複数トラックでガイドが重ならない・「ガイドに合わせる」が使えない（issue #32）。
//
//   (1) ガイドのトラック自身を編集中は重ならない。その理由をステータス行と「ガイドに合わせる」（G）に出す
//   (2) ガイドを指定していないときも理由を出す。別のトラックを選べば重なる
//   (3) 外した最初のテイクをドロップで足し直すと、前の編集が戻る（新しい空のプロジェクトにしない）
//   (4) 最初のテイクを外したままテイクを開き直すと先頭に戻り、それは Ctrl+Z で外せる（取り消しの履歴に入る）
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'projects', '_test-multitrack');
const MEDIA = path.join(ROOT, 'Media');
const TAKE = path.join(MEDIA, 'take.wav');
const GUIDE = path.join(MEDIA, 'guide.wav');
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
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(MEDIA, { recursive: true });
  fs.copyFileSync(M.clip('C'), TAKE);
  fs.copyFileSync(M.clip('C2'), GUIDE);
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--guide', GUIDE, '--project-dir', PROJECT,
      '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console: ${m.text()}`);
  });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null,
    { timeout: 240000 });
}

const tracks = () => win.evaluate(() => window.__app.tracks());
const status = () => win.evaluate(() => window.__app.status());
const guideShown = () => win.evaluate(() => !!(window.__app.S.vd?.guide && window.__app.S.vd?.guide_basis));

async function pressG() {
  await win.locator('#roll').click({ position: { x: 5, y: 5 } }).catch(() => {});
  await win.keyboard.press('Escape');
  await win.keyboard.press('g');
}

test('(1) ガイドのトラック自身を編集中は重ならず、理由をステータス行と「ガイドに合わせる」に出す', async () => {
  await settle();
  const [take, guide] = await tracks();
  expect(guide.guide).toBe(true);
  expect(await guideShown()).toBe(true);
  await win.evaluate((id) => window.__app.selectTrack(id), guide.id);
  await settle();
  expect(await guideShown()).toBe(false);
  expect(await status()).toContain('ガイドは重ならない: 編集中のトラックがガイド。別のトラックを選ぶと重なる');
  await pressG();
  expect(await status()).toBe('ガイドに合わせる…: 今は使えない（編集中のトラックがガイド。別のトラックを選ぶと重なる）');
  expect(await win.locator('#bMacro').isDisabled()).toBe(true);
  // 別のトラックを選べば重なる
  await win.evaluate((id) => window.__app.selectTrack(id), take.id);
  await settle();
  expect(await guideShown()).toBe(true);
  expect(await status()).toContain(`/ ガイド: ${path.basename(GUIDE)}`);
});

test('(2) ガイドを指定していないとき・編集中のトラックをガイドにしたときも理由を出す', async () => {
  const [take, guide] = await tracks();
  await win.evaluate(() => window.__app.setGuide(null));
  await settle();
  expect(await guideShown()).toBe(false);
  await pressG();
  expect(await status()).toContain('今は使えない（トラックの見出しのガイドのアイコンでガイドを指定する）');
  // 編集中のトラック自身をガイドにした
  await win.evaluate((id) => window.__app.setGuide(id), take.id);
  await settle();
  expect(await status()).toContain('（今のトラックには重ならない: 編集中のトラックがガイド。別のトラックを選ぶと重なる）');
  await win.evaluate((id) => window.__app.setGuide(id), guide.id);
  await settle();
  expect(await guideShown()).toBe(true);
  expect(await status()).toBe(`ガイド: ${guide.name}`);
});

test('(3) 外した最初のテイクをドロップで足し直すと、前の編集が戻る', async () => {
  const [take, guide] = await tracks();
  // 最初のテイクに編集を 1 つ入れる
  const nid = await win.evaluate(() => window.__app.notes()[0].id);
  await win.evaluate(async (id) => {
    await window.api.call('shift_pitch', { note_id: id, cents: 40, author: 'human' });
    await window.__app.refresh({ keepView: true });
  }, nid);
  await settle();
  const cents = await win.evaluate((id) => window.__app.notes().find((n) => n.id === id).cents, nid);
  expect(Math.round(cents)).toBe(40);
  // ガイドのトラックへ移って、最初のテイクを外す
  await win.evaluate((id) => window.__app.selectTrack(id), guide.id);
  await settle();
  await win.evaluate((id) => window.__app.removeTrack(id), take.id);
  await settle();
  expect((await tracks()).map((t) => t.name)).toEqual([guide.name]);
  // 同じファイルを落とす → 足して編集対象に。前の編集（40 セント）がそのまま
  await win.evaluate((p) => window.__app.openDropped([p]), TAKE);
  await settle();
  const ts = await tracks();
  const back = ts.find((t) => t.name === take.name);
  expect(back.current).toBe(true);
  expect(path.resolve(back.project_dir)).toBe(path.resolve(PROJECT));
  const c2 = await win.evaluate((id) => window.__app.notes().find((n) => n.id === id)?.cents, nid);
  expect(Math.round(c2)).toBe(40);
  expect(await guideShown()).toBe(true);
});

test('(4) 最初のテイクを外したままテイクを開き直すと先頭に戻り、Ctrl+Z で外せる', async () => {
  const ts0 = await tracks();
  const take = ts0.find((t) => t.name === 'take');
  const guide = ts0.find((t) => t.name === 'guide');
  await win.evaluate((id) => window.__app.selectTrack(id), guide.id);
  await settle();
  await win.evaluate((id) => window.__app.removeTrack(id), take.id);
  await settle();
  // 開き直す（テイクを開く・最近使ったプロジェクトと同じ open_project。置き場はこのテストのもの）
  await win.evaluate(([p, d]) => window.__app.openProject(p, null, { keepView: false, projectDir: d }),
    [TAKE, PROJECT]);
  await settle();
  const ts = await tracks();
  expect(ts.map((t) => t.name)).toEqual(['take', 'guide']);
  expect(ts[0].current).toBe(true);
  expect(await win.evaluate(() => window.__app.undoTitle())).toContain('元に戻す: 最初のテイクを戻す');
  await win.evaluate(() => window.__app.undo());           // Ctrl+Z と同じ
  await settle();
  await win.waitForFunction(() => window.__app.tracks().length === 1, null, { timeout: 60000 });
  expect((await tracks()).map((t) => t.name)).toEqual(['guide']);
  expect(errors).toEqual([]);
});
