// アプリ名 Gliss（名前・アイコン・ロゴ）と、旧名（vocal-editor）の設定の引き継ぎ（issue #29）。
//
//   (N1) ウィンドウのタイトル・アプリ名・ヘルプ > Gliss について・アイコンのファイル
//   (N2) 旧名の userData の state.json を新しい userData に写す（最近使ったプロジェクト・キーの設定・
//        前回の表示範囲・前回のテイク）。元のファイルは書き換えない
//   (N3) もう新しい側に設定があれば写さない（旧い側が変わっても上書きしない）
//   (N4) `--user-data-dir` だけ（テスト）なら旧い場所を見ない（人の設定をテストに持ち込まない）
//
// 人の実際の userData（%APPDATA%\vocal-editor-app・%APPDATA%\Gliss）には触れない:
// 旧い側も新しい側も projects/_test-name-* に作って `--legacy-user-data-dir` で指す。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const TAKE = M.clip('C');
const ROOT = path.join(REPO, 'projects', '_test-name');
const PROJECT = path.join(ROOT, 'project');
const LEGACY = path.join(ROOT, 'legacy-userdata');     // 旧名のときの userData のつもり
const USERDATA = path.join(ROOT, 'userdata');          // Gliss の userData のつもり
const FRESH = path.join(ROOT, 'fresh-userdata');       // (N4)

const LEGACY_STATE = {
  lastTake: TAKE,
  lastGuide: null,
  recent: [{ take: TAKE, guide: null, at: 1760000000000 }],
  keys: { merge: ['Q'], semitone: [] },
  view: { t0: 2.2, span: 1.3, take: TAKE, pv: null, trackH: 60 },
  bounds: { x: 60, y: 60, width: 1200, height: 740 },
};

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

async function launch(args) {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  app = await electron.launch({ args: [APP, '--project-dir', PROJECT, '--mute', ...args], env });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
}
async function close() {
  await app?.close();
  app = null;
}
const readJson = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
// タイトルバーのメニューバー（main の Menu を写したものを titlebar.js が描いている）
const menuBar = () => win.evaluate(() => window.__app.menubar().model.map((m) => ({
  label: m.label,
  items: m.submenu.filter((x) => x.type !== 'separator').map((x) => ({
    label: x.label, role: x.role || null, accel: x.accelerator || null,
    sub: x.submenu ? x.submenu.map((y) => y.label) : null,
  })),
})));

test.beforeAll(async () => {
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(LEGACY, { recursive: true });
  fs.writeFileSync(path.join(LEGACY, 'state.json'), JSON.stringify(LEGACY_STATE, null, 2), 'utf8');
});

test.afterAll(async () => {
  await close();
});

test('(N2) 旧名の設定を写す（最近使ったもの・キー・表示範囲・前回のテイク）。元は書き換えない', async () => {
  const before = fs.readFileSync(path.join(LEGACY, 'state.json'));
  await launch(['--user-data-dir', USERDATA, '--legacy-user-data-dir', LEGACY]);
  // 前回のテイク（lastTake）が引き継がれて、起動するとそれが開く
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  const st = readJson(path.join(USERDATA, 'state.json'));
  expect(st.migratedFrom).toBe(LEGACY);
  expect(st.recent[0].take).toBe(TAKE);
  expect(st.keys).toEqual(LEGACY_STATE.keys);
  expect(st.lastTake).toBe(TAKE);
  // 前回の表示範囲
  const v = await win.evaluate(() => window.__app.view());
  expect(v.t0).toBeCloseTo(2.2, 3);
  expect(v.span).toBeCloseTo(1.3, 3);
  // キーの設定（メニューバーの表記）と最近使ったプロジェクト
  await expect.poll(async () => (await menuBar()).find((m) => m.label === 'ノート')?.items
    .find((x) => x.label === '結合')?.accel).toBe('Q');
  const file = (await menuBar()).find((m) => m.label === 'ファイル').items;
  expect(file.find((x) => x.label === '最近使ったプロジェクト').sub).toContain(path.basename(TAKE));
  // 元のファイルはそのまま
  expect(fs.readFileSync(path.join(LEGACY, 'state.json')).equals(before)).toBe(true);
});

test('(N1) ウィンドウのタイトル・アプリ名・ヘルプ > Gliss について・アイコン', async () => {
  // タイトルは「プロジェクトの名前 — Gliss」（issue #33。未保存なら名前の後に *。旧形式は自動で保存なので * は付かない）
  expect(await win.title()).toBe(`${M.stem(TAKE)} — Gliss`);
  const info = await app.evaluate(({ app: a, BrowserWindow }) => ({
    name: a.getName(), title: BrowserWindow.getAllWindows()[0].getTitle(),
  }));
  expect(info).toEqual({ name: 'Gliss', title: `${M.stem(TAKE)} — Gliss` });
  expect(await win.locator('#mock').getAttribute('aria-label')).toBe('Gliss');
  const mb = await menuBar();
  const help = mb.find((m) => m.label === 'ヘルプ');
  expect(help.items).toEqual([{ label: 'AI とつなぐ…', role: null, accel: null, sub: null },
    { label: 'モデルと追加の機能…', role: null, accel: null, sub: null },
    { label: '更新を確認…', role: null, accel: null, sub: null },
    { label: 'Gliss について', role: 'about', accel: null, sub: null }]);
  // ウィンドウとタスクバーのアイコン（main が BrowserWindow の icon に渡す）・About のアイコン
  for (const f of ['gliss.ico', 'gliss-icon-256.png', 'gliss-wordmark.svg', 'gliss-wordmark-light.svg']) {
    expect(fs.existsSync(path.join(REPO, 'assets', 'logo', f))).toBe(true);
  }
  expect(fs.readFileSync(path.join(REPO, 'assets', 'logo', 'gliss.ico')).subarray(0, 4))
    .toEqual(Buffer.from([0, 0, 1, 0]));
  // Electron が読める（空の画像にならない）
  const sizes = await app.evaluate(({ nativeImage }, dir) => ['gliss.ico', 'gliss-icon-256.png']
    .map((f) => nativeImage.createFromPath(`${dir}/${f}`).getSize()), path.join(REPO, 'assets', 'logo'));
  for (const sz of sizes) expect(sz.width).toBeGreaterThanOrEqual(16);
  expect(readJson(path.join(APP, 'package.json'))).toMatchObject({ name: 'gliss', productName: 'Gliss' });
  // MCP のサーバー名（画面も .mcp.json の gliss を読んでエンジンを起動している）
  expect(Object.keys(readJson(path.join(REPO, '.mcp.json')).mcpServers)).toEqual(['gliss']);
  await close();
});

test('(N3) 新しい側に設定があれば写さない（旧い側が変わっても上書きしない）', async () => {
  const mine = readJson(path.join(USERDATA, 'state.json'));
  fs.writeFileSync(path.join(LEGACY, 'state.json'),
    JSON.stringify({ ...LEGACY_STATE, keys: { merge: ['W'] }, recent: [] }, null, 2), 'utf8');
  await launch(['--user-data-dir', USERDATA, '--legacy-user-data-dir', LEGACY]);
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await expect.poll(async () => (await menuBar()).find((m) => m.label === 'ノート')?.items
    .find((x) => x.label === '結合')?.accel).toBe('Q');
  const st = readJson(path.join(USERDATA, 'state.json'));
  expect(st.keys).toEqual(mine.keys);
  expect(st.migratedFrom).toBe(LEGACY);
  expect(st.recent.length).toBeGreaterThan(0);
  await close();
});

test('(N4) --user-data-dir だけなら旧い場所を見ない', async () => {
  // --take を渡す（渡さないと、前回のテイクが無いのでファイルを選ぶダイアログが出る）
  await launch(['--take', TAKE, '--user-data-dir', FRESH]);
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  const st = readJson(path.join(FRESH, 'state.json'));
  expect(st.migratedFrom).toBeUndefined();
  expect(st.keys).toBeUndefined();
  await expect.poll(async () => (await menuBar()).find((m) => m.label === 'ノート')?.items
    .find((x) => x.label === '結合')?.accel).toBe('CmdOrCtrl+J');
  await close();
});

test('エラーが出ていない', async () => {
  expect(errors).toEqual([]);
});
