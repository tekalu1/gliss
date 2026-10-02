// 新規プロジェクト・開く・保存（issue #33。DAW と同じファイル操作）。
//
//   (D1) メニューバー: ファイル > 新規プロジェクト（Ctrl+N）・開く…（Ctrl+O）・最近使ったプロジェクト・保存（Ctrl+S）・
//        名前を付けて保存…（Ctrl+Shift+S）
//   (D2) 何も開いていない起動 → 新規（空）→ WAV を落とすとトラックに。無題は「無題* — Gliss」
//   (D3) 保存（無題なので名前を付けて保存のダイアログ）→ .gliss ができて「*」が消える。編集で「*」、Ctrl+S で消える
//   (D4) 閉じる前の「保存しますか」: 保存しない → 閉じる。次に開くと最後に保存した中身
//   (D5) 保存しないまま落ちた → 次に開くと続きから（「保存していない変更の続きから開いた」）
//   (D6) 旧形式（projects/…）はそのまま開ける（自動で保存・「*」は出ない）。名前を付けて保存すると .gliss になる
//
// ダイアログ（保存先・「保存しますか」）は main プロセスの dialog を差し替えて答える（人の操作は要らない）。
// 作業場所（VOCAL_ENGINE_WORK_DIR）・旧形式の置き場（VOCAL_ENGINE_PROJECTS）・userData はこのテストの下に分ける。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2', 'B');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'projects', '_test-document');
const MEDIA = path.join(ROOT, 'Song', 'Media');
const TAKE = path.join(MEDIA, 'take.wav');
const GUIDE = path.join(MEDIA, 'guide.wav');
const LEGACY_TAKE = path.join(ROOT, 'Old', 'take2.wav');
const GLISS = path.join(ROOT, 'Song', 'うた.gliss');
const USERDATA = path.join(ROOT, 'userdata');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

function env() {
  const e = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete e.ELECTRON_RUN_AS_NODE;
  e.VOCAL_ENGINE_WORK_DIR = path.join(ROOT, 'work');
  e.VOCAL_ENGINE_PROJECTS = path.join(ROOT, 'projects');
  return e;
}

async function launch(args = []) {
  app = await electron.launch({ args: [APP, '--user-data-dir', USERDATA, '--mute', ...args], env: env() });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => !!window.__app, null, { timeout: 60000 });
}

/** ウィンドウを閉じる（「保存しますか」が出たら answer で答える）。閉じ終わるまで待つ。 */
async function closeApp(answer = 'discard') {
  if (!app) return;
  await stubAsk(answer);
  const done = app.waitForEvent('close', { timeout: 60000 }).catch(() => {});
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]?.close());
  await done;
  app = null;
}

/** 「保存しますか」の答え（保存 / 保存しない / キャンセル）を決めておく。聞かれた名前を控える。 */
async function stubAsk(answer) {
  await app.evaluate(({ dialog }, a) => {
    globalThis.__asked = [];
    dialog.showMessageBox = async (_w, o) => {
      globalThis.__asked.push(o.message);
      return { response: { save: 0, discard: 1, cancel: 2 }[a] };
    };
  }, answer);
}
const asked = () => app.evaluate(() => globalThis.__asked || []);

/** 名前を付けて保存のダイアログの答え。 */
async function stubSaveTo(p) {
  await app.evaluate(({ dialog }, q) => {
    globalThis.__saveAsked = [];
    dialog.showSaveDialog = async (_w, o) => {
      globalThis.__saveAsked.push(o.defaultPath);
      return { canceled: !q, filePath: q || undefined };
    };
  }, p);
}

async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 240000 });
}
const doc = () => win.evaluate(() => window.__app.doc());
const title = () => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getTitle());
// タイトルバーのメニューバー（main の Menu を写したものを titlebar.js が描いている）
const menuBar = () => win.evaluate(() => window.__app.menubar().model.map((m) => ({
  label: m.label,
  items: m.submenu.filter((x) => x.type !== 'separator').map((x) => ({
    label: x.label, accel: x.accelerator || null, enabled: x.enabled,
    sub: x.submenu ? x.submenu.map((y) => y.label) : null,
  })),
})));

async function editOnce(cents = 40) {
  await win.evaluate(async (c) => {
    const id = window.__app.notes()[0].id;
    await window.api.call('shift_pitch', { note_id: id, cents: c, author: 'human' });
    await window.__app.refresh({ keepView: true });
  }, cents);
  await settle();
}

test.beforeAll(async () => {
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(MEDIA, { recursive: true });
  fs.mkdirSync(path.dirname(LEGACY_TAKE), { recursive: true });
  fs.copyFileSync(M.clip('C'), TAKE);
  fs.copyFileSync(M.clip('C2'), GUIDE);
  fs.copyFileSync(M.clip('B'), LEGACY_TAKE);
  await launch();
});

test.afterAll(async () => {
  await closeApp('discard');
  expect(errors).toEqual([]);
});

test('(D1) ファイル メニュー: 新規・開く・最近使ったプロジェクト・保存・名前を付けて保存', async () => {
  await expect.poll(async () => (await menuBar()).find((m) => m.label === 'ファイル')?.items
    .map((x) => [x.label, x.accel]).slice(0, 5)).toEqual([
    ['新規プロジェクト', 'CmdOrCtrl+N'], ['開く…', 'CmdOrCtrl+O'], ['最近使ったプロジェクト', null],
    ['保存', 'CmdOrCtrl+S'], ['名前を付けて保存…', 'CmdOrCtrl+Shift+S'],
  ]);
  // 何も開いていない: 保存は押せない、タイトルはアプリ名だけ
  const file = (await menuBar()).find((m) => m.label === 'ファイル').items;
  expect(file.find((x) => x.label === '保存').enabled).toBe(false);
  expect(await title()).toBe('Gliss');
  await expect(win.locator('#firstRun')).toBeVisible();
  expect(await win.evaluate(() => window.__app.status())).toBe('');
});

test('(D2) 新規（空）→ WAV を落とすとトラックに。無題は「無題* — Gliss」', async () => {
  await win.evaluate(() => window.__app.onMenu({ cmd: 'new-project' }));
  await settle();
  expect(await doc()).toMatchObject({ kind: 'untitled', name: '無題', dirty: false, tracks: 0 });
  expect(await win.evaluate(() => window.__app.status())).toContain('新しいプロジェクト');
  expect(await title()).toBe('無題 — Gliss');
  await win.evaluate((p) => window.__app.openDropped([p]), TAKE);
  await settle();
  await win.evaluate((p) => window.__app.openDropped([p], { guide: true }), GUIDE);
  await settle();
  const ts = await win.evaluate(() => window.__app.tracks());
  expect(ts.map((t) => [t.name, t.guide, t.current])).toEqual([
    ['take', false, true], ['guide', true, false]]);
  expect(await win.evaluate(() => window.__app.ready())).toBe(true);
  await expect.poll(() => title()).toBe('無題* — Gliss');
  // 何も開いていないところへ落とす経路は D6 の後（新しいプロジェクトのテイクとして開く）
});

test('(D3) 保存（名前を付けて保存）→ .gliss。編集で「*」、Ctrl+S で消える', async () => {
  await stubSaveTo(GLISS);
  expect(await win.evaluate(() => window.__app.onMenu({ cmd: 'save' }))).toBe(true);
  await settle();
  // 既定の保存先は最初のトラックの音声の隣
  expect(await app.evaluate(() => globalThis.__saveAsked)).toEqual([path.join(MEDIA, 'take.gliss')]);
  expect(fs.existsSync(GLISS)).toBe(true);
  const d = JSON.parse(fs.readFileSync(GLISS, 'utf8'));
  expect(d.format).toBe('gliss-project');
  expect(d.session.tracks.map((t) => t.rel)).toEqual(['Media/take.wav', 'Media/guide.wav']);
  expect(await doc()).toMatchObject({ kind: 'gliss', path: GLISS, name: 'うた', dirty: false });
  await expect.poll(() => title()).toBe('うた — Gliss');
  expect(await win.evaluate(() => window.__app.status())).toContain('保存した');
  // 編集すると「*」
  await editOnce(40);
  await expect.poll(() => title()).toBe('うた* — Gliss');
  // Ctrl+S（ダイアログは出ない）
  await stubSaveTo(null);
  await win.locator('#roll').click({ position: { x: 5, y: 5 } }).catch(() => {});
  await win.keyboard.press('Escape');
  await win.keyboard.press('Control+s');
  await expect.poll(() => title()).toBe('うた — Gliss');
  expect(await app.evaluate(() => globalThis.__saveAsked)).toEqual([]);
  // 最近使ったプロジェクトに入る
  const file = (await menuBar()).find((m) => m.label === 'ファイル').items;
  expect(file.find((x) => x.label === '最近使ったプロジェクト').sub).toContain('うた');
  // 編集対象の切り替えでは「*」にならない
  const ts = await win.evaluate(() => window.__app.tracks());
  await win.evaluate((id) => window.__app.selectTrack(id), ts[1].id);
  await settle();
  await win.evaluate((id) => window.__app.selectTrack(id), ts[0].id);
  await settle();
  expect((await doc()).dirty).toBe(false);
});

test('(D4) 閉じる前の「保存しますか」→ 保存しない。次に開くと最後に保存した中身', async () => {
  await editOnce(-30);
  await expect.poll(() => title()).toBe('うた* — Gliss');
  const before = await win.evaluate(() => window.__app.notes()[0].cents);
  expect(Math.round(before)).toBe(10);
  // キャンセル → 閉じない
  await stubAsk('cancel');
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  await expect.poll(() => asked()).toEqual(['「うた」の変更を保存しますか？']);
  expect(await title()).toBe('うた* — Gliss');
  // 保存しない → 閉じる
  await closeApp('discard');
  await launch();                                     // 前回のプロジェクト（.gliss）が開く
  await win.waitForFunction(() => window.__app.ready(), null, { timeout: 240000 });
  await settle();
  expect(await doc()).toMatchObject({ kind: 'gliss', path: GLISS, dirty: false });
  expect(Math.round(await win.evaluate(() => window.__app.notes()[0].cents))).toBe(40);
});

test('(D5) 保存しないまま落ちた → 次に開くと続きから', async () => {
  await editOnce(20);
  await expect.poll(() => title()).toBe('うた* — Gliss');
  // 落ちたのと同じ（close を通らずに終わる）
  const done = app.waitForEvent('close', { timeout: 60000 }).catch(() => {});
  await app.evaluate(({ app: a }) => { setTimeout(() => a.exit(0), 10); });
  await done;
  app = null;
  await launch();
  await win.waitForFunction(() => window.__app.ready(), null, { timeout: 240000 });
  await settle();
  expect(await doc()).toMatchObject({ kind: 'gliss', path: GLISS, dirty: true });
  expect(await win.evaluate(() => window.__app.status())).toContain('保存していない変更の続きから開いた');
  expect(Math.round(await win.evaluate(() => window.__app.notes()[0].cents))).toBe(60);
  // 保存しておく（次のテストのため）
  await stubSaveTo(null);
  expect(await win.evaluate(() => window.__app.saveDoc())).toBe(true);
  expect((await doc()).dirty).toBe(false);
});

test('(D6) 旧形式はそのまま開ける（「*」は出ない）。名前を付けて保存すると .gliss になる', async () => {
  // 旧形式のプロジェクトを作る（issue #33 より前と同じ: テイクの WAV を開くと projects/<名前>-<sha8>/ に自動で保存）
  await win.evaluate(async (p) => {
    await window.api.call('close_project', {});
    await window.api.call('open_project', { take_path: p, author: 'human' });
    await window.api.call('analyze_take', {});
    const vd = await window.api.call('list_notes', { kind: 'note', limit: 1 });
    await window.api.call('shift_pitch', { note_id: vd.notes[0].id, cents: 25, author: 'human' });
    await window.api.call('close_project', {});
  }, LEGACY_TAKE);
  const legacyDirs = fs.readdirSync(path.join(ROOT, 'projects'));
  expect(legacyDirs.length).toBe(1);
  // その WAV を開く（開く… で音声を選んだのと同じ）→ 旧形式のプロジェクトが開く
  await win.evaluate((p) => window.__app.loadProject(p), LEGACY_TAKE);
  await settle();
  expect(await doc()).toMatchObject({ kind: 'legacy', name: 'take2', dirty: false });
  expect(Math.round(await win.evaluate(() => window.__app.notes()[0].cents))).toBe(25);
  expect(await win.evaluate(() => window.__app.status())).toContain('旧形式のプロジェクト');
  await editOnce(10);
  expect((await doc()).dirty).toBe(false);          // 自動で保存している
  expect(await title()).toBe('take2 — Gliss');
  // 保存（Ctrl+S）= 名前を付けて保存
  const out = path.join(ROOT, 'Old', 'ふるい.gliss');
  await stubSaveTo(out);
  expect(await win.evaluate(() => window.__app.onMenu({ cmd: 'save' }))).toBe(true);
  await settle();
  expect(await doc()).toMatchObject({ kind: 'gliss', path: out, dirty: false });
  expect(Math.round(await win.evaluate(() => window.__app.notes()[0].cents))).toBe(35);
  const moved = JSON.parse(fs.readFileSync(path.join(ROOT, 'projects', legacyDirs[0], 'moved_to.json'), 'utf8'));
  expect(moved.gliss).toBe(out);
  // 同じテイクを新しいプロジェクトとして開くと（何も開いていないところへ落とした・起動引数の WAV と同じ経路）、
  // 保存した .gliss が開く
  await win.evaluate(() => window.__app.newProject());
  await settle();
  expect(await doc()).toMatchObject({ kind: 'untitled', tracks: 0 });
  await win.evaluate((p) => window.__app.newProject({ take: p }), LEGACY_TAKE);
  await settle();
  expect(await doc()).toMatchObject({ kind: 'gliss', path: out });
  expect(Math.round(await win.evaluate(() => window.__app.notes()[0].cents))).toBe(35);
});
