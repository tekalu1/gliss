import { test, expect, _electron as electron } from '@playwright/test';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { modelDirectory, totalModelBytes } from '../model-download.mjs';
import { engineLaunch } from '../paths.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ENGINE = path.join(REPO, 'engine');
// フィクスチャの zip を作る python。エンジンと同じもの（.mcp.json か <repo>/.venv）。GLISS_TEST_PYTHON で差し替えられる
const PYTHON = process.env.GLISS_TEST_PYTHON || engineLaunch({ packaged: false, repo: REPO, bridge: '' }).command;
const SHOTS = process.env.GLISS_SCREENSHOT_DIR || path.join(os.tmpdir(), 'gliss-first-run-shots');

test.describe.configure({ mode: 'serial' });

test('保存先（配布版・開発版とも LOCALAPPDATA）と明示的な保存先', () => {
  expect(totalModelBytes).toBe(590802801);
  const localAppData = path.join(os.tmpdir(), 'gliss-local-app-data');
  expect(modelDirectory({ packaged: true, env: {}, localAppData, repo: REPO }))
    .toBe(path.join(localAppData, 'Gliss', 'models'));
  expect(modelDirectory({ packaged: false, env: {}, localAppData, repo: REPO }))
    .toBe(path.join(localAppData, 'Gliss', 'models'));
  expect(modelDirectory({ packaged: true, env: { VOCAL_ENGINE_MODELS_DIR: path.join(localAppData, 'custom') },
    localAppData, repo: REPO })).toBe(path.join(localAppData, 'custom'));
});

test('空の画面からタイトルバーと AI 接続を使える', async () => {
  const f = await fixture();
  try {
    fs.mkdirSync(SHOTS, { recursive: true });
    await expect(f.win.locator('#titlebar')).toBeVisible();
    await expect(f.win.locator('#menubar button', { hasText: 'ヘルプ' })).toBeVisible();
    await expect(f.win.locator('.tb')).toBeHidden();
    await expect(f.win.locator('#firstRun .fr-head')).toHaveCount(0);
    const regions = await f.win.evaluate(() => ({
      bar: getComputedStyle(document.querySelector('#titlebar')).getPropertyValue('-webkit-app-region').trim(),
      menu: getComputedStyle(document.querySelector('#menubar button')).getPropertyValue('-webkit-app-region').trim(),
    }));
    expect(regions).toEqual({ bar: 'drag', menu: 'no-drag' });
    const help = f.win.locator('#menubar button', { hasText: 'ヘルプ' });
    await expect(f.win.locator('#firstRunAi')).toBeVisible();
    await f.win.screenshot({ path: path.join(SHOTS, 'integ-first-run.png') });
    await help.click();
    await expect(f.win.locator('.mbd .l', { hasText: 'AI とつなぐ…' })).toBeVisible();
    await f.win.screenshot({ path: path.join(SHOTS, 'integ-help-menu.png') });
    await f.win.locator('.mbd button', { hasText: 'AI とつなぐ…' }).click();
    await expect(f.win.locator('#ai')).toBeVisible();
    await f.win.screenshot({ path: path.join(SHOTS, 'integ-ai-dialog.png') });
    await f.win.keyboard.press('Escape');
    await f.win.locator('#firstRunAi').click();
    await expect(f.win.locator('#ai')).toBeVisible();
    await f.win.keyboard.press('Escape');
    await f.win.locator('#mock').focus();
    await f.win.keyboard.press('Alt');
    await expect.poll(() => f.win.evaluate(() => window.__app.menubar().active)).toBe(true);
    for (let i = 0; i < 4; i++) await f.win.keyboard.press('ArrowRight');
    await f.win.keyboard.press('ArrowDown');
    await expect(f.win.locator('.mbd button', { hasText: 'AI とつなぐ…' })).toBeVisible();
    await f.win.keyboard.press('Enter');
    await expect(f.win.locator('#ai')).toBeVisible();
  } finally { await f.close(); }
});

async function fixture({ corrupt = false, failFirst = false, slow = false } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-first-run-test-'));
  const archives = {};
  for (const id of ['rmvpe', 'hubertfa']) {
    const zip = path.join(dir, `${id}.zip`);
    execFileSync(PYTHON, [path.join(HERE, 'fixture_zip.py'), zip, id]);
    archives[id] = fs.readFileSync(zip);
  }
  let failing = failFirst;
  const server = http.createServer((req, res) => {
    const id = req.url.slice(1).replace('.zip', '');
    if (!archives[id]) { res.writeHead(404).end(); return; }
    if (failing) { res.writeHead(503).end(); return; }
    const body = archives[id];
    res.writeHead(200, { 'content-type': 'application/zip', 'content-length': body.length });
    if (slow) {
      res.write(body.subarray(0, Math.floor(body.length / 2)));
      setTimeout(() => res.end(body.subarray(Math.floor(body.length / 2))),
        typeof slow === 'number' ? slow : 1500);
    } else res.end(body);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  const sources = Object.fromEntries(Object.entries(archives).map(([id, body]) => [id, {
    url: `${url}/${id}.zip`, size: body.length,
    sha256: corrupt && id === 'rmvpe' ? '0'.repeat(64) : crypto.createHash('sha256').update(body).digest('hex'),
  }]));
  const models = path.join(dir, 'models');
  fs.mkdirSync(models);
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1',
    VOCAL_ENGINE_CWD: ENGINE, PYTHONPATH: ENGINE, VOCAL_ENGINE_MODELS_DIR: models,
    GLISS_MODEL_SOURCE_JSON: JSON.stringify(sources), VOCAL_EDITOR_MUTE: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  const app = await electron.launch({ args: [APP, '--user-data-dir', path.join(dir, 'userdata'), '--mute'], env });
  const win = await app.firstWindow();
  await expect(win.locator('#firstRun')).toBeVisible();
  await expect(win.locator('#modelDownloadButton')).toBeVisible();
  return { dir, models, win, app, setFail: (v) => { failing = v; },
    close: async () => {
      await app.close();
      await new Promise((resolve) => server.close(resolve));
      fs.rmSync(dir, { recursive: true, force: true });
    } };
}

test('重み無し・ダウンロード中・準備完了', async () => {
  const f = await fixture({ slow: true });
  try {
    fs.mkdirSync(SHOTS, { recursive: true });
    await f.win.screenshot({ path: path.join(SHOTS, 'first-run-missing.png') });
    await f.win.locator('#modelLicenseButton').click();
    await expect(f.win.locator('#modelLicense')).toContainText('重みは未確認');
    await expect(f.win.locator('#modelLicense a')).toHaveCount(2);
    await f.win.locator('#modelLicenseButton').click();
    await f.win.locator('#modelDownloadButton').click();
    await expect(f.win.locator('#modelProgress')).toBeVisible();
    await expect(f.win.locator('#firstRunDrop')).toBeVisible();
    await expect(f.win.locator('#firstRunOpen')).toBeEnabled();
    await f.win.screenshot({ path: path.join(SHOTS, 'first-run-downloading.png') });
    await expect(f.win.locator('#modelSummary')).toHaveText('準備完了', { timeout: 30000 });
    await f.win.screenshot({ path: path.join(SHOTS, 'first-run-ready.png') });
    expect(fs.existsSync(path.join(f.models, 'rmvpe.onnx'))).toBe(true);
    expect(fs.existsSync(path.join(f.models, 'hubertfa', '1218_hfa_model_new_dict', 'model.onnx'))).toBe(true);
    await expect(f.win.locator('#modelDownloadButton')).toBeHidden();
    await expect(f.win.locator('#firstRunDrop')).toBeVisible();
  } finally { await f.close(); }
});

test('通信失敗後に再試行できる', async () => {
  const f = await fixture({ failFirst: true });
  try {
    await f.win.locator('#modelDownloadButton').click();
    await expect(f.win.locator('#modelError')).toContainText('HTTP 503');
    await expect(f.win.locator('#modelDownloadButton')).toHaveText('再試行');
    f.setFail(false);
    await f.win.locator('#modelDownloadButton').click();
    await expect(f.win.locator('#modelSummary')).toHaveText('準備完了', { timeout: 30000 });
  } finally { await f.close(); }
});

test('SHA-256 不一致を拒否する', async () => {
  const f = await fixture({ corrupt: true });
  try {
    await f.win.locator('#modelDownloadButton').click();
    await expect(f.win.locator('#modelError')).toContainText('SHA-256 が一致しない');
    expect(fs.existsSync(path.join(f.models, 'rmvpe.onnx'))).toBe(false);
  } finally { await f.close(); }
});

test('取り消すとダウンロードを止める', async () => {
  const f = await fixture({ slow: 10000 });
  try {
    await f.win.locator('#modelDownloadButton').click();
    await expect(f.win.locator('#modelCancelButton')).toBeVisible();
    await f.win.locator('#modelCancelButton').click();
    await expect(f.win.locator('#modelDownloadButton')).toBeVisible();
    expect(fs.existsSync(path.join(f.models, 'rmvpe.onnx'))).toBe(false);
  } finally { await f.close(); }
});
