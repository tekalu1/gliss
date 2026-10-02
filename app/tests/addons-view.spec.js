// 任意機能のアドオンの画面（初回画面の行・ヘルプ > モデルと追加の機能…。renderer/addons.js・main の addons.mjs）。
// 開発版で GLISS_ADDONS_DIR（一時フォルダ）を渡し、小さな偽のアドオン（site-packages に .py が 1 つ）を
// ローカルの HTTP サーバーから配る。エンジンは開発の venv の python（互換の判定は venv の numpy の版と照らす）。
//
//   (G1) ヘルプにある。初回画面に行が出て、ダイアログは同じ行（解析モデル・アドオン）を移して出し、閉じたら戻す
//   (G2) ダウンロード → その場でエンジンが読む（再起動しない）→ 読み込む前の削除はその場で消える
//   (G3) SHA-256 が合わないものは入れない（再試行が出る）
//   (G4) エンジンと合わないアドオンは読まれず「更新が必要です」→ 更新で取り直す
import { test, expect, _electron as electron } from '@playwright/test';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { engineLaunch } from '../paths.mjs';
import { storeZip } from './unit/store-zip.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const ENGINE = path.join(path.dirname(APP), 'engine');
// エンジンと同じ python（.mcp.json か <repo>/.venv）。GLISS_TEST_PYTHON で差し替えられる
const PYTHON = process.env.GLISS_TEST_PYTHON || engineLaunch({ packaged: false, repo: path.dirname(APP), bridge: '' }).command;
const SHOTS = process.env.GLISS_SCREENSHOT_DIR || path.join(os.tmpdir(), 'gliss-addons-shots');
const ROW = '[data-addon="lyrics-ja"]';

test.describe.configure({ mode: 'serial' });

/** 開発の venv で互換になる manifest（Python の版・OS・numpy の版をそろえる）。 */
function manifest(o = {}) {
  const [numpy, plat, py] = execFileSync(PYTHON, ['-c',
    'import numpy, sysconfig, sys; print(numpy.__version__, sysconfig.get_platform().replace("-", "_"), "cp%d%d" % sys.version_info[:2])'],
  { encoding: 'utf8' }).trim().split(' ');
  return { format: 1, id: 'lyrics-ja', title: '漢字の歌詞の読み', key: 'testkey00001', python: py, platform: plat,
    requires: { numpy }, packages: { 'gliss-fake-addon': '1' }, modules: ['gliss_fake_addon'], ...o };
}

async function fixture({ corrupt = false, preinstall = null } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-addons-test-'));
  const addons = path.join(dir, 'addons');
  fs.mkdirSync(addons);
  const m = manifest();
  const zip = path.join(dir, `Gliss-addon-lyrics-ja-${m.key}.zip`);
  storeZip(zip, { 'gliss-addon.json': JSON.stringify(m), 'LICENSES.txt': 'fake MIT\n',
    'site-packages/gliss_fake_addon.py': 'VALUE = 1\n' });
  const body = fs.readFileSync(zip);
  const catalog = path.join(dir, 'catalog.json');
  fs.writeFileSync(catalog, JSON.stringify({ addons: [{ id: 'lyrics-ja', title: '漢字の歌詞の読み', key: m.key,
    file: path.basename(zip), size: body.length, installedSize: 2e6,
    sha256: corrupt ? '0'.repeat(64) : crypto.createHash('sha256').update(body).digest('hex'),
    licenses: [{ name: 'pyopenjtalk-plus', license: 'MIT' }, { name: 'Open JTalk', license: 'BSD-3-Clause' }] }] }));
  if (preinstall) {
    fs.mkdirSync(path.join(addons, 'lyrics-ja', 'site-packages'), { recursive: true });
    fs.writeFileSync(path.join(addons, 'lyrics-ja', 'gliss-addon.json'), JSON.stringify(manifest(preinstall)));
  }
  const server = http.createServer((req, res) => {
    if (req.url !== `/${path.basename(zip)}`) { res.writeHead(404).end(); return; }
    res.writeHead(200, { 'content-type': 'application/zip', 'content-length': body.length });
    res.end(body);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const models = path.join(dir, 'models');
  fs.mkdirSync(models);
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_ENGINE_CWD: ENGINE, PYTHONPATH: ENGINE,
    VOCAL_ENGINE_MODELS_DIR: models, VOCAL_EDITOR_MUTE: '1', GLISS_ADDONS_DIR: addons, GLISS_ADDON_CATALOG: catalog,
    GLISS_ADDON_BASE_URL: `http://127.0.0.1:${server.address().port}` };
  delete env.ELECTRON_RUN_AS_NODE;
  const app = await electron.launch({ args: [APP, '--user-data-dir', path.join(dir, 'userdata'), '--mute'], env });
  const win = await app.firstWindow();
  await expect(win.locator('#firstRun')).toBeVisible();
  await expect(win.locator(`#addonRows ${ROW}`)).toBeVisible();
  // 削除の確認（OS のダイアログ）は「削除」を押したことにする
  await app.evaluate(({ dialog }) => { dialog.showMessageBox = async () => ({ response: 0 }); });
  return { dir, addons, win, app,
    info: () => win.evaluate(() => window.api.call('engine_info', {})),
    close: async () => {
      await app.close();
      await new Promise((resolve) => server.close(resolve));
      fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
    } };
}

const summary = (win) => win.locator(`${ROW} [data-r="summary"]`);

test('(G1) ヘルプ > モデルと追加の機能… と初回画面の行', async () => {
  const f = await fixture();
  try {
    fs.mkdirSync(SHOTS, { recursive: true });
    const help = await f.win.evaluate(() => window.__app.appMenuTemplate().find((m) => m.label === 'ヘルプ').submenu
      .filter((x) => x.label).map((x) => x.label));
    expect(help).toEqual(['AI とつなぐ…', 'モデルと追加の機能…', '更新を確認…', 'Gliss について']);
    await expect(summary(f.win)).toHaveText(/^任意 · 約 \d+ MB$/);
    await f.win.screenshot({ path: path.join(SHOTS, 'addons-first-run.png') });
    await f.win.evaluate(() => window.__app.runCommand('addons'));
    await expect(f.win.locator('#addonsDlg')).toBeVisible();
    // 初回画面の行をダイアログへ移して出す（同じ要素。解析モデルの行も）
    expect(await f.win.evaluate(() => ['#modelRow', '#modelLicense', '#addonRows']
      .map((s) => document.querySelector(s).parentElement.id))).toEqual(['addonsBody', 'addonsBody', 'addonsBody']);
    await f.win.locator(`#addonsDlg ${ROW} [data-b="license"]`).click();
    await expect(f.win.locator('#addonsDlg [data-license="lyrics-ja"]')).toContainText('Open JTalk：BSD-3-Clause');
    await f.win.screenshot({ path: path.join(SHOTS, 'addons-dialog.png') });
    await f.win.keyboard.press('Escape');
    await expect(f.win.locator('#addonsDlg')).toBeHidden();
    expect(await f.win.evaluate(() => [...document.querySelector('#firstRun').children].map((e) => e.id).slice(0, 4)))
      .toEqual(['modelRow', 'modelLicense', 'addonRows', 'firstRunDrop']);
  } finally { await f.close(); }
});

test('(G2) ダウンロード → その場で読む → 削除', async () => {
  const f = await fixture();
  try {
    expect((await f.info()).addons.installed).toEqual([]);
    await expect(summary(f.win)).toHaveText(/^任意/);
    await expect(f.win.locator(`${ROW} [data-b="download"]`)).toBeEnabled();
    await f.win.locator(`${ROW} [data-b="download"]`).click({ timeout: 10000 });
    await expect(summary(f.win)).toHaveText(/^使えます/, { timeout: 60000 });
    await f.win.screenshot({ path: path.join(SHOTS, 'addons-ready.png') });
    const st = (await f.info()).addons.installed[0];
    expect(st).toMatchObject({ id: 'lyrics-ja', active: true, compatible: true, source: 'addon', key: 'testkey00001' });
    expect(fs.readdirSync(path.join(f.addons, 'lyrics-ja')).sort()).toEqual(['LICENSES.txt', 'gliss-addon.json', 'site-packages']);
    expect(fs.readdirSync(f.addons)).toEqual(['lyrics-ja']);           // 展開の一時フォルダが残らない
    await expect(f.win.locator(`${ROW} [data-b="remove"]`)).toBeVisible();
    await f.win.locator(`${ROW} [data-b="remove"]`).click();
    await expect(summary(f.win)).toHaveText(/^任意/);
    expect(fs.existsSync(path.join(f.addons, 'lyrics-ja'))).toBe(false);
    expect((await f.info()).addons.installed).toEqual([]);
  } finally { await f.close(); }
});

test('(G3) SHA-256 が合わないものは入れない', async () => {
  const f = await fixture({ corrupt: true });
  try {
    await f.win.locator(`${ROW} [data-b="download"]`).click();
    await expect(f.win.locator(`${ROW} [data-r="error"]`)).toContainText('SHA-256 が一致しない');
    await expect(f.win.locator(`${ROW} [data-b="retry"]`)).toBeVisible();
    expect(fs.readdirSync(f.addons)).toEqual([]);
  } finally { await f.close(); }
});

test('(G4) エンジンと合わないアドオンは読まず、更新で取り直す', async () => {
  const f = await fixture({ preinstall: { key: 'oldkey000000', requires: { numpy: '0.0.1' } } });
  try {
    await expect(summary(f.win)).toHaveText(/^更新が必要です · 約 \d+ MB$/);
    await expect(summary(f.win)).toHaveAttribute('title', /numpy 0\.0\.1/);
    const st = (await f.info()).addons.installed[0];
    expect(st).toMatchObject({ active: false, compatible: false });
    await f.win.screenshot({ path: path.join(SHOTS, 'addons-incompatible.png') });
    await f.win.locator(`${ROW} [data-b="update"]`).click();
    await expect(summary(f.win)).toHaveText(/^使えます/, { timeout: 60000 });
    expect((await f.info()).addons.installed[0]).toMatchObject({ active: true, key: 'testkey00001' });
  } finally { await f.close(); }
});
