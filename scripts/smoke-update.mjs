// 自動更新の通し確認（インストールはしない）。古い版の展開版を、ローカルの HTTP サーバーに置いた新しい版へ向けて、
// 確認 → 見つける → ダウンロード → SHA-512 の検証 → 「再起動して更新」の表示まで進むことを確かめる。
// **「再起動して更新」（quitAndInstall）は押さない**（押すとインストーラが走る）。
//
//   node scripts/smoke-update.mjs --old <古い版の win-unpacked> --new <新しい版の成果物のフォルダ> [--shots <フォルダ>] [--rate <MB/s。既定 40>] [--timeout <ms>]
//
//   --old  展開版（Gliss.exe）。electron-builder --dir の出力には resources/app-update.yml が無いので、そのときは足して終わったら消す。
//          版は --new より古いこと
//   --new  electron-builder（nsis）の出力（latest.yml・インストーラ・.blockmap）
// 作り方の例（app/ で）:
//   electron-builder --dir -c.directories.output=<tmp>/old --publish never
//   electron-builder --win nsis -c.extraMetadata.version=0.1.0-beta.2 -c.directories.output=<tmp>/new --publish never
//
// 確かめること:
//   1. 古い版の resources/app-update.yml を generic provider（http://127.0.0.1:<port>/）に差し替えて起動すると、
//      ヘルプ > 更新を確認… で新しい版を見つけ、ダウンロードし、downloaded（「再起動して更新」）になる。
//      取得したインストーラ（%LOCALAPPDATA%\gliss-updater\pending）の SHA-512 が latest.yml と一致する
//   2. latest.yml の SHA-512 が合わないときは、検証の失敗の文（利用者向け）で error になり、生のエラーを出さない
//   3. インストール先のエンジン exe で動くプロセス（AI クライアントが起動したものの代わりに 2 つ起動する）を
//      engine-processes.mjs で見つけて止められる
// %LOCALAPPDATA%（更新のキャッシュ・重みの置き場）と userData は一時フォルダに差し替える。app-update.yml は終わったら戻す。
import { execFileSync, spawn } from 'node:child_process';
import { Transform } from 'node:stream';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const appRequire = createRequire(path.join(ROOT, 'app', 'package.json'));
const { _electron: electron } = appRequire('@playwright/test');
const yaml = appRequire(appRequire.resolve('js-yaml', { paths: [path.dirname(appRequire.resolve('electron-updater/package.json'))] }));
const { findEngineProcesses, stopEngineProcesses } = await import(pathToFileURL(path.join(ROOT, 'app', 'engine-processes.mjs')).href);
const { MESSAGES } = await import(pathToFileURL(path.join(ROOT, 'app', 'updates.mjs')).href);

const argv = process.argv.slice(2);
const opt = (name) => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : null; };
const OLD = path.resolve(opt('--old') || '');
const NEW = path.resolve(opt('--new') || '');
const SHOTS = opt('--shots') ? path.resolve(opt('--shots')) : null;
const TIMEOUT_MS = Number(opt('--timeout') || 600000);
// 配る速さ（localhost だと 1 秒で終わり、ダウンロード中の画面を見られない）
const RATE = Number(opt('--rate') || 40) * 1024 * 1024;

/** 1 秒あたり RATE バイトに絞って流す。 */
function throttle() {
  const t0 = Date.now();
  let sent = 0;
  return new Transform({
    transform(chunk, _enc, done) {
      sent += chunk.length;
      const wait = (sent / RATE) * 1000 - (Date.now() - t0);
      setTimeout(() => done(null, chunk), Math.max(0, wait));
    },
  });
}
const EXE = path.join(OLD, 'Gliss.exe');
const UPDATE_YML = path.join(OLD, 'resources', 'app-update.yml');
const ENGINE_DIR = path.join(OLD, 'resources', 'engine', 'vocal-engine');

const report = { scenarios: {} };
const fail = (m) => { throw new Error(m); };
const launched = new Set();
const sha512 = (file) => crypto.createHash('sha512').update(fs.readFileSync(file)).digest('base64');

function killTree(pid) {
  if (!pid) return;
  try { execFileSync('taskkill.exe', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' }); } catch { /* もう無い */ }
}

async function closeApp(app, win) {
  const pid = app.process().pid;
  try { await win?.evaluate(() => window.api.setDoc({ name: 'smoke', dirty: false })); } catch { /* 閉じかけ */ }
  let timer;
  const closed = await Promise.race([
    app.close().then(() => true, () => false),
    new Promise((resolve) => { timer = setTimeout(() => resolve(false), 15000); }),
  ]);
  clearTimeout(timer);
  if (!closed) report.forcedKill = true;
  killTree(pid);
  launched.delete(pid);
}

// ---------------------------------------------------------------- 配る側
const latest = yaml.load(fs.readFileSync(path.join(NEW, 'latest.yml'), 'utf8'));
let mode = 'ok';
const served = [];
const server = http.createServer((req, res) => {
  const name = decodeURIComponent(new URL(req.url, 'http://x').pathname.slice(1));
  served.push(`${req.method} ${name}${req.headers.range ? ` (${req.headers.range})` : ''}`);
  if (name === 'latest.yml') {
    const data = mode === 'corrupt'
      ? { ...latest, sha512: crypto.createHash('sha512').update('違う中身').digest('base64'),
        files: latest.files.map((f) => ({ ...f, sha512: crypto.createHash('sha512').update('違う中身').digest('base64') })) }
      : latest;
    res.writeHead(200, { 'content-type': 'text/yaml' }).end(yaml.dump(data));
    return;
  }
  const file = path.join(NEW, path.basename(name));
  if (!name || path.basename(name) !== name || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  const size = fs.statSync(file).size;
  const m = /^bytes=(\d+)-(\d*)$/.exec(req.headers.range || '');
  if (m) {
    const start = Number(m[1]); const end = m[2] ? Number(m[2]) : size - 1;
    res.writeHead(206, { 'content-length': end - start + 1, 'content-range': `bytes ${start}-${end}/${size}`, 'accept-ranges': 'bytes' });
    fs.createReadStream(file, { start, end }).pipe(throttle()).pipe(res);
    return;
  }
  res.writeHead(200, { 'content-length': size, 'accept-ranges': 'bytes' });
  fs.createReadStream(file).pipe(throttle()).pipe(res);
});

// ---------------------------------------------------------------- 受け取る側（古い版）
async function scenario(name, { serve, expect: want }) {
  mode = serve;
  served.length = 0;
  const r = { phases: [] };
  report.scenarios[name] = r;
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-update-smoke-'));
  const local = path.join(tmp, 'LocalAppData');
  fs.mkdirSync(local, { recursive: true });
  const env = { ...process.env, LOCALAPPDATA: local, VOCAL_EDITOR_MUTE: '1', VOCAL_EDITOR_HIDDEN: '1',
    VOCAL_EDITOR_IGNORE_MOUSE: '1', ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  for (const k of ['ELECTRON_RUN_AS_NODE', 'VOCAL_ENGINE_CWD', 'VOCAL_ENGINE_MODELS_DIR', 'VOCAL_ENGINE_MODELS', 'PYTHONPATH']) delete env[k];
  const app = await electron.launch({ executablePath: EXE, args: ['--user-data-dir', path.join(tmp, 'userdata'), '--mute'], env });
  launched.add(app.process().pid);
  let win = null;
  try {
    win = await app.firstWindow();
    const errors = [];
    win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
    r.main = await app.evaluate(({ app: a }) => ({ isPackaged: a.isPackaged, version: a.getVersion() }));
    await win.waitForFunction(() => window.__app?.updates && window.__app.updates().phase !== 'unavailable', null, { timeout: 60000 })
      .catch(() => fail(`更新が有効にならない: ${JSON.stringify(report.scenarios[name])}`));
    r.initial = await win.evaluate(() => window.__app.updates());
    // ヘルプ > 更新を確認…（開いて、すぐに確かめる）
    const t0 = Date.now();
    // 押すだけ（確かめて取得し終わるまで待たない。途中の段階を下で記録する）
    await win.evaluate(() => { window.__app.runCommand('check-updates'); });
    let last = null;
    let shot = false;
    for (;;) {
      const s = await win.evaluate(() => window.__app.updates());
      const key = `${s.phase}${s.phase === 'downloading' ? ` ${s.progress}%` : ''}`;
      if (key !== last) { r.phases.push(key); last = key; }
      // ダウンロードの途中の画面（ダイアログ・タイトルバーの知らせ）
      if (SHOTS && !shot && s.phase === 'downloading' && s.progress >= 30) {
        shot = true;
        fs.mkdirSync(SHOTS, { recursive: true });
        await win.screenshot({ path: path.join(SHOTS, `update-${name}-downloading.png`) });
      }
      if (['downloaded', 'error', 'current'].includes(s.phase)) { r.final = s; break; }
      if (Date.now() - t0 > 300000) fail(`5 分で終わらない: ${r.phases.join(' → ')}`);
      await new Promise((ok) => setTimeout(ok, 100));
    }
    r.seconds = (Date.now() - t0) / 1000;
    r.dialog = await win.evaluate(() => ({ st: document.querySelector('#updSt').textContent,
      button: document.querySelector('#updAct').hidden ? null : document.querySelector('#updAct').textContent }));
    if (SHOTS) {
      fs.mkdirSync(SHOTS, { recursive: true });
      await win.screenshot({ path: path.join(SHOTS, `update-${name}-dialog.png`) });
      await win.keyboard.press('Escape');
      await win.screenshot({ path: path.join(SHOTS, `update-${name}-titlebar.png`) });
    }
    r.note = await win.evaluate(() => (document.querySelector('#updNote').hidden ? null : document.querySelector('#updNote').innerText));
    r.served = [...served];
    r.log = fs.readFileSync(path.join(tmp, 'userdata', 'updates.log'), 'utf8').split(/\r?\n/).filter(Boolean)
      .map((l) => l.replace(/^\S+ /, '').slice(0, 300));
    r.errors = errors;
    if (r.final.phase !== want) fail(`${name}: ${want} にならない（${r.final.phase}: ${r.final.error}）`);
    if (want === 'downloaded') {
      if (r.final.target !== latest.version) fail(`見つけた版が違う: ${r.final.target}`);
      if (r.dialog.button !== '再起動して更新') fail(`「再起動して更新」が出ない: ${JSON.stringify(r.dialog)}`);
      const pending = path.join(local, 'gliss-updater', 'pending');
      const files = fs.readdirSync(pending);
      const installer = files.find((f) => f.endsWith('.exe'));
      if (!installer) fail(`取得したインストーラが無い: ${files}`);
      r.pending = { dir: pending, files, sha512Matches: sha512(path.join(pending, installer)) === latest.sha512,
        bytes: fs.statSync(path.join(pending, installer)).size };
      if (!r.pending.sha512Matches) fail('取得したインストーラの SHA-512 が latest.yml と違う');
    } else if (want === 'error') {
      if (r.final.error !== MESSAGES.verify) fail(`検証の失敗の文が違う: ${r.final.error}`);
      if (r.note) fail(`失敗が知らせに出ている: ${r.note}`);
    }
  } finally {
    await closeApp(app, win);
    try { fs.rmSync(tmp, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 }); } catch { /* 掴まれていたら一時フォルダに残る */ }
  }
}

/** AI クライアントが起動したエンジンの代わりに、同じ exe を 2 つ起動して、見つけて止める。 */
async function stopEngines() {
  const r = {};
  report.scenarios.engines = r;
  const exe = path.join(ENGINE_DIR, 'vocal-engine.exe');
  const kids = [0, 1].map(() => spawn(exe, [], { stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true,
    env: { ...process.env, LOCALAPPDATA: os.tmpdir() } }));
  for (const k of kids) launched.add(k.pid);
  const until = Date.now() + 30000;
  let found = [];
  while (Date.now() < until) {
    found = await findEngineProcesses(ENGINE_DIR);
    if (kids.every((k) => found.includes(k.pid))) break;
    await new Promise((ok) => setTimeout(ok, 300));
  }
  r.found = found;
  r.spawned = kids.map((k) => k.pid);
  if (!kids.every((k) => found.includes(k.pid))) fail(`起動したエンジンが見つからない: ${found}`);
  // 隣のフォルダ（同じ名前で始まる別の場所）は拾わない
  r.otherDir = await findEngineProcesses(`${ENGINE_DIR}-other`);
  if (r.otherDir.length) fail(`別のフォルダのつもりで拾った: ${r.otherDir}`);
  const t0 = Date.now();
  r.result = await stopEngineProcesses(ENGINE_DIR);
  r.seconds = (Date.now() - t0) / 1000;
  r.after = await findEngineProcesses(ENGINE_DIR);
  if (r.after.length) fail(`止まらないエンジンがある: ${r.after}`);
}

async function main() {
  if (!fs.existsSync(EXE)) fail(`${EXE} が無い（--old に electron-builder --dir の出力を渡す）`);
  if (!fs.existsSync(path.join(NEW, 'latest.yml'))) fail(`${NEW}/latest.yml が無い（--new に electron-builder の出力を渡す）`);
  // electron-builder --dir の出力には app-update.yml が無い（nsis の出力の win-unpacked にはある）。無ければ足して、終わったら消す
  const original = fs.existsSync(UPDATE_YML) ? fs.readFileSync(UPDATE_YML, 'utf8') : null;
  const cfg = original ? yaml.load(original) : { updaterCacheDirName: 'gliss-updater' };
  report.originalUpdateConfig = original ? cfg : null;
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok));
  const url = `http://127.0.0.1:${server.address().port}/`;
  fs.writeFileSync(UPDATE_YML, yaml.dump({ provider: 'generic', url, updaterCacheDirName: cfg.updaterCacheDirName }), 'utf8');
  report.latest = { version: latest.version, path: latest.path, size: latest.files?.[0]?.size };
  try {
    await scenario('ok', { serve: 'ok', expect: 'downloaded' });
    await scenario('corrupt', { serve: 'corrupt', expect: 'error' });
    await stopEngines();
  } finally {
    if (original === null) fs.rmSync(UPDATE_YML, { force: true });
    else fs.writeFileSync(UPDATE_YML, original, 'utf8');
    server.close();
  }
}

const watchdog = setTimeout(() => {
  for (const pid of launched) killTree(pid);
  console.error(JSON.stringify(report, null, 2));
  console.error(`失敗: ${TIMEOUT_MS / 1000} 秒で終わらなかった`);
  process.exit(2);
}, TIMEOUT_MS);

try {
  await main();
  console.log(JSON.stringify(report, null, 2));
  clearTimeout(watchdog);
  process.exit(0);
} catch (e) {
  for (const pid of launched) killTree(pid);
  console.error(JSON.stringify(report, null, 2));
  console.error(`失敗: ${e.message}`);
  process.exit(1);
}
