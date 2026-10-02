// 自動更新の画面（ヘルプ > 更新を確認…・右下の知らせ。renderer/updates.js）。
// 開発版は更新できない（unavailable）。ほかの状態は main から画面へ送る状態（updates-state）を差し込んで見る
// （取得・検証・再起動して更新の本物の流れは scripts/smoke-update.mjs が配布版で確かめる）。
//
//   (V1) ヘルプ > 更新を確認… でダイアログが開く。開発版は「この版は自動で更新できません」、設定は押せない
//   (V2) 知らせ: 新しい版がある（自動ダウンロードがオフ）→ ダウンロード中 → 再起動して更新。失敗は知らせに出さない
//   (V3) 更新した後の最初の起動: 「<版> に更新しました」→「内容」でこの版のリリースノート
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ENGINE = path.join(REPO, 'engine');
const SHOTS = process.env.GLISS_SCREENSHOT_DIR || path.join(os.tmpdir(), 'gliss-updates-shots');
const VERSION = JSON.parse(fs.readFileSync(path.join(APP, 'package.json'), 'utf8')).version;

test.describe.configure({ mode: 'serial' });

let app;
let win;
let dir;

test.beforeAll(async () => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-updates-view-'));
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_ENGINE_CWD: ENGINE, PYTHONPATH: ENGINE,
    VOCAL_EDITOR_MUTE: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  app = await electron.launch({ args: [APP, '--user-data-dir', path.join(dir, 'userdata'), '--mute'], env });
  win = await app.firstWindow();
  await win.waitForFunction(() => window.__app?.menubar().model.some((m) => m.label === 'ヘルプ'), null, { timeout: 120000 });
  fs.mkdirSync(SHOTS, { recursive: true });
});

test.afterAll(async () => {
  await app?.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

/** main が画面へ送る状態を差し込む（main の updates.mjs の snapshot と同じ形）。 */
const push = (s) => app.evaluate(({ BrowserWindow }, st) => BrowserWindow.getAllWindows()[0].webContents.send('updates-state', st),
  { version: VERSION, enabled: true, channel: 'beta', autoCheck: true, autoDownload: true, target: null, progress: null,
    error: null, updatedFrom: null, ...s });

test('(V1) ヘルプ > 更新を確認…（開発版は更新できない）', async () => {
  const help = await win.evaluate(() => window.__app.menubar().model.find((m) => m.label === 'ヘルプ').submenu
    .filter((x) => x.type !== 'separator').map((x) => x.label));
  expect(help).toEqual(['AI とつなぐ…', 'モデルと追加の機能…', '更新を確認…', 'Gliss について']);
  await win.evaluate(() => window.__app.runCommand('check-updates'));
  await expect(win.locator('#upd')).toBeVisible();
  await expect(win.locator('#updVer')).toHaveText(VERSION);
  await expect(win.locator('#updSt')).toHaveText('この版は自動で更新できません');
  await expect(win.locator('#updAct')).toBeHidden();
  for (const k of ['autoCheck', 'autoDownload', 'beta']) await expect(win.locator(`[data-pref="${k}"]`)).toBeDisabled();
  // 版が beta なので既定でベータ版を受け取る
  await expect(win.locator('[data-pref="beta"]')).toBeChecked();
  await expect(win.locator('#updNote')).toBeHidden();
  await win.screenshot({ path: path.join(SHOTS, 'updates-dev.png') });
  await win.keyboard.press('Escape');
  await expect(win.locator('#upd')).toBeHidden();
});

test('(V2) 知らせ: 見つけた → ダウンロード中 → 再起動して更新・失敗は知らせない', async () => {
  await push({ phase: 'available', target: '0.1.0-beta.2', autoDownload: false });
  await expect(win.locator('#updNote .t')).toHaveText('新しい版 0.1.0-beta.2 があります');
  await expect(win.locator('#updNote [data-b="act"]')).toHaveText('ダウンロード');
  await expect(win.locator('#updNote [data-b="dismiss"]')).toBeHidden();
  await win.screenshot({ path: path.join(SHOTS, 'updates-note-available.png') });

  await push({ phase: 'downloading', target: '0.1.0-beta.2', progress: 42 });
  await expect(win.locator('#updNote .t')).toHaveText('更新をダウンロードしています 42%');
  await expect(win.locator('#updNote [data-b="act"]')).toBeHidden();
  await win.locator('#updNote .t').click();
  await expect(win.locator('#upd')).toBeVisible();
  await expect(win.locator('#updSt')).toHaveText('0.1.0-beta.2 をダウンロードしています 42%');
  await expect(win.locator('#updBar')).toBeVisible();
  await expect(win.locator('#updBar')).toHaveAttribute('aria-valuenow', '42');
  await expect(win.locator('[data-pref="beta"]')).toBeDisabled();      // 取得の途中はチャネルを変えない
  await expect(win.locator('[data-pref="autoCheck"]')).toBeEnabled();
  await win.screenshot({ path: path.join(SHOTS, 'updates-downloading.png') });

  await push({ phase: 'downloaded', target: '0.1.0-beta.2', progress: 100 });
  await expect(win.locator('#updSt')).toHaveText('0.1.0-beta.2 の準備ができました');
  await expect(win.locator('#updAct')).toHaveText('再起動して更新');
  await expect(win.locator('#updAct')).toBeEnabled();
  await expect(win.locator('#updBar')).toBeHidden();
  await win.screenshot({ path: path.join(SHOTS, 'updates-downloaded-dialog.png') });
  await win.keyboard.press('Escape');
  await expect(win.locator('#updNote .t')).toHaveText('0.1.0-beta.2 の準備ができました');
  await expect(win.locator('#updNote [data-b="act"]')).toHaveText('再起動して更新');
  await win.screenshot({ path: path.join(SHOTS, 'updates-note-downloaded.png') });

  await push({ phase: 'error', error: '更新を確認できませんでした。インターネットの接続を確かめて、しばらくしてからもう一度お試しください' });
  await expect(win.locator('#updNote')).toBeHidden();
  await win.evaluate(() => window.__app.openUpdates());
  await expect(win.locator('#updSt')).toHaveText(/インターネットの接続/);
  await expect(win.locator('#updSt')).toHaveClass(/err/);
  await expect(win.locator('#updAct')).toHaveText('確認');
  await win.screenshot({ path: path.join(SHOTS, 'updates-error.png') });
  await win.keyboard.press('Escape');
  await push({ phase: 'current' });
  await expect(win.locator('#updNote')).toBeHidden();
});

test('(V3) 更新した後の最初の起動: 「<版> に更新しました」と、この版の内容', async () => {
  const notes = JSON.parse(fs.readFileSync(path.join(REPO, 'releases', `${VERSION}.json`), 'utf8'));
  test.skip(!fs.existsSync(path.join(APP, 'release-info.json')), 'app/release-info.json が無い（node scripts/release-notes.mjs で作る）');
  await push({ phase: 'current', updatedFrom: '0.1.0-beta.0' });
  await expect(win.locator('#updNote .t')).toHaveText(`${VERSION} に更新しました`);
  await expect(win.locator('#updNote [data-b="act"]')).toHaveText('内容');
  await expect(win.locator('#updNote [data-b="dismiss"]')).toBeVisible();
  await win.screenshot({ path: path.join(SHOTS, 'updates-note-updated.png') });
  await win.locator('#updNote [data-b="act"]').click();
  await expect(win.locator('#upd')).toBeVisible();
  await expect(win.locator('#updNotes')).toBeVisible();
  await expect(win.locator('#updNotes .nh')).toHaveText(`${VERSION} · ${notes.date} · ${notes.title}`);
  await expect(win.locator('#updNotes li')).toHaveCount(notes.sections.reduce((n, s) => n + s.items.length, 0));
  await win.screenshot({ path: path.join(SHOTS, 'updates-notes.png') });
  // 「内容」を押したら知らせは閉じる（main の dismissNotice）
  await expect(win.locator('#updNote')).toBeHidden();
  await win.keyboard.press('Escape');
});

test('(V4) 再起動して更新の前の「保存しますか」: main が頼み、画面が confirmDiscard の答えを返す', async () => {
  // main の installUpdate が使う往復（confirm-update → app.answer）。未保存の変更が無ければすぐ true が返る。
  // 未保存のときは main の「保存しますか」のダイアログになる（OS のダイアログなので、ここでは押さない）
  await win.evaluate(() => window.__app.openUpdates());
  const answer = await app.evaluate(({ BrowserWindow, ipcMain }) => new Promise((resolve) => {
    const id = 987654;
    const on = (_e, rid, ok) => { if (rid === id) { ipcMain.removeListener('app.answer', on); resolve(ok); } };
    ipcMain.on('app.answer', on);
    BrowserWindow.getAllWindows()[0].webContents.send('confirm-update', id);
  }));
  expect(answer).toBe(true);
  // 確かめる前にダイアログは閉じる（「保存しますか」の後ろに残さない）
  await expect(win.locator('#upd')).toBeHidden();
});
