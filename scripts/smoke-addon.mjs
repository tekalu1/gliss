// 配布版（electron-builder --dir の展開版）で、任意機能のアドオン（漢字の歌詞の読み）の取得・読み込み・互換・削除を通しで確かめる。
//
//   node scripts/smoke-addon.mjs --models <重みのフォルダ> [--exe <Gliss.exe>] [--shots <フォルダ>] [--rate <MB/s。既定 25>] [--timeout <ms。既定 600000>]
//
//   前提: node scripts/build-addon.mjs lyrics-ja（engine/packaging/dist-addons/）→ pnpm dist:dir（目録を埋めた展開版）
//   --models: RMVPE と HubertFA の重みのフォルダ（開発機の %LOCALAPPDATA%\Gliss\models など。読むだけ。VOCAL_ENGINE_MODELS_DIR で渡す）
//
// アドオンはローカルの HTTP サーバー（127.0.0.1・空いているポート）から配り、GLISS_ADDON_BASE_URL で取得先を差し替える。
// LOCALAPPDATA と userData は一時フォルダ（利用者の %LOCALAPPDATA%\Gliss・%APPDATA%\Gliss には触れない）。WAV は合成。
//
//   1. アドオン無し: エンジンは何も読まない。漢字の歌詞は「アドオンを入れる」案内のエラー（かなは動く）
//   2. 画面（ヘルプ > モデルと追加の機能…）からダウンロード → その場で読み直し → 漢字の歌詞の音素が出る
//   3. 起動し直す: 起動時にアドオンを読む。AI クライアントに登録するのと同じ exe を単体で起動しても読む（--check）
//   4. 削除: 使っている（.pyd を読み込んだ）ので「次に起動したときに削除」→ 起動し直すと消えている
//   5. 互換の無いアドオン（共有する numpy の版が違う manifest）を置く: 読まず「合いません。更新してください」→ 更新で取り直す
//      → 読み込む前なら削除はその場で済む → もう一度取得して漢字が読める
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const appRequire = createRequire(path.join(ROOT, 'app', 'package.json'));
const { _electron: electron } = appRequire('@playwright/test');
const extract = appRequire('extract-zip');

const argv = process.argv.slice(2);
const opt = (name) => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : null; };
const EXE = path.resolve(opt('--exe') || path.join(ROOT, 'app', 'dist', 'win-unpacked', 'Gliss.exe'));
const MODELS = opt('--models') ? path.resolve(opt('--models')) : null;
const SHOTS = opt('--shots') ? path.resolve(opt('--shots')) : null;
const RATE = Number(opt('--rate') || 25) * 1e6;
const TIMEOUT_MS = Number(opt('--timeout') || 600000);
const DIST = path.join(ROOT, 'engine', 'packaging', 'dist-addons');
const KANJI = '夜空に歌う';

const report = { steps: [] };
const fail = (m) => { throw new Error(m); };
const step = (name, data) => { report.steps.push({ name, ...data }); console.error(`[smoke-addon] ${name}`); };
let launchedPid = null;

/** 合成の歌（smoke-packaged.mjs と同じ。A3→C4→E4 のビブラート付きの倍音列）。 */
function synthWav(file, sr = 44100) {
  const notes = [[220, 1.0], [0, 0.25], [261.63, 1.0], [0, 0.25], [329.63, 1.2]];
  const total = notes.reduce((t, [, d]) => t + d, 0);
  const pcm = new Int16Array(Math.round(total * sr));
  let i = 0; let phase = 0;
  for (const [f0, dur] of notes) {
    const n = Math.round(dur * sr);
    for (let k = 0; k < n && i < pcm.length; k += 1, i += 1) {
      if (!f0) continue;
      const t = k / sr;
      const env = Math.min(1, t / 0.03, (dur - t) / 0.05);
      phase += (2 * Math.PI * f0 * (1 + 0.006 * Math.sin(2 * Math.PI * 5.5 * t))) / sr;
      let v = 0;
      for (let h = 1; h <= 8; h += 1) v += Math.sin(h * phase) / h;
      pcm[i] = Math.round(Math.max(-1, Math.min(1, 0.35 * env * v)) * 32767);
    }
  }
  const head = Buffer.alloc(44);
  head.write('RIFF', 0); head.writeUInt32LE(36 + pcm.length * 2, 4); head.write('WAVEfmt ', 8);
  head.writeUInt32LE(16, 16); head.writeUInt16LE(1, 20); head.writeUInt16LE(1, 22);
  head.writeUInt32LE(sr, 24); head.writeUInt32LE(sr * 2, 28); head.writeUInt16LE(2, 32); head.writeUInt16LE(16, 34);
  head.write('data', 36); head.writeUInt32LE(pcm.length * 2, 40);
  fs.writeFileSync(file, Buffer.concat([head, Buffer.from(pcm.buffer)]));
}

/** dist-addons を配る HTTP サーバー（速さを絞って、ダウンロード中の画面を撮れるようにする）。 */
function serve(dir, rate) {
  const log = [];
  const server = http.createServer((req, res) => {
    const name = decodeURIComponent(new URL(req.url, 'http://x').pathname.slice(1));
    const file = path.join(dir, path.basename(name));
    log.push(`${req.method} /${name}`);
    if (!name || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
    const size = fs.statSync(file).size;
    res.writeHead(200, { 'content-length': size, 'content-type': 'application/zip' });
    if (req.method === 'HEAD') { res.end(); return; }
    const fd = fs.openSync(file, 'r');
    const chunk = Buffer.alloc(1 << 20);
    let pos = 0;
    const pump = () => {
      if (res.destroyed) { fs.closeSync(fd); return; }
      const n = fs.readSync(fd, chunk, 0, chunk.length, pos);
      if (!n) { fs.closeSync(fd); res.end(); return; }
      pos += n;
      res.write(Buffer.from(chunk.subarray(0, n)));
      setTimeout(pump, (n / rate) * 1000);
    };
    res.on('close', () => { if (pos < size) try { fs.closeSync(fd); } catch { /* 閉じ済み */ } });
    pump();
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port, log })));
}

function killTree(pid) {
  if (!pid) return;
  try { execFileSync('taskkill.exe', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' }); } catch { /* もう無い */ }
}

async function closeApp(app, win) {
  const pid = app.process().pid;
  try { await win?.evaluate(() => window.api.setDoc({ name: 'smoke', dirty: false })); } catch { /* 閉じかけ */ }
  let timer;
  await Promise.race([app.close().catch(() => {}), new Promise((r) => { timer = setTimeout(r, 15000); })]);
  clearTimeout(timer);
  killTree(pid);
}

async function shot(win, name) {
  if (!SHOTS) return;
  await win.waitForTimeout(300);
  await win.screenshot({ path: path.join(SHOTS, name) });
}

/** 起動して、合成の WAV を開いて解析が終わるまで待つ。 */
async function launch(ctx) {
  const app = await electron.launch({ executablePath: EXE, args: ['--user-data-dir', ctx.userData, '--mute', '--take', ctx.wav], env: ctx.env });
  launchedPid = app.process().pid;
  const win = await app.firstWindow();
  win.on('pageerror', (e) => ctx.errors.push(`pageerror: ${e.message}`));
  win.on('console', (m) => { if (m.type() === 'error') ctx.errors.push(`console: ${m.text()}`); });
  if (!await app.evaluate(({ app: a }) => a.isPackaged)) fail('配布版として起動していない');
  await win.waitForFunction(() => !!window.__app?.ready?.(), null, { timeout: 240000 });
  // 削除の確認（OS のダイアログ）は「削除」を押したことにする
  await app.evaluate(({ dialog }) => { dialog.showMessageBox = async () => ({ response: 0 }); });
  return { app, win };
}

const info = (win, reload = false) => win.evaluate((r) => window.api.call('engine_info', r ? { reload_addons: true } : {}), reload);
const row = (win) => win.evaluate(() => window.__app.addons().addons.find((a) => a.id === 'lyrics-ja'));

/** 漢字の歌詞を入れてみる（set_lyrics → 音素アラインメント）: { ok, kana, phonemes, warnings } */
async function tryKanji(win) {
  const r = await win.evaluate((t) => window.api.call('set_lyrics', { text: t, author: 'human' }), KANJI);
  if (r.ok === false) return { ok: false, error: String(r.error).slice(0, 300) };
  const ph = await win.evaluate(() => window.api.call('get_phonemes', {}));
  const labels = (ph.phonemes || []).filter((p) => p.label !== 'silence').map((p) => p.text || p.label);
  const kana = r.kana || '';
  return { ok: labels.length > 0 && !!kana && !/[㐀-鿿]/.test(kana), kana, phonemes: labels.join(' '),
    warnings: r.warnings || [], g2p: ph.g2p?.source };
}

async function openDialog(win) {
  await win.evaluate(() => window.__app.openAddons());
  await win.locator('#addonsDlg').waitFor({ state: 'visible' });
  await win.waitForFunction(() => window.__app.addons().addons.length > 0 && !['unknown'].includes(window.__app.addons().addons[0].state));
}

async function waitRow(win, pred, timeout = 120000) {
  try {
    await win.waitForFunction(pred, null, { timeout, polling: 200 });
  } catch (e) {
    fail(`${e.message.split('\n')[0]}: ${JSON.stringify(await win.evaluate(() => window.__app.addons()))}`);
  }
  return row(win);
}

async function main() {
  if (!fs.existsSync(EXE)) fail(`${EXE} が無い（pnpm dist:dir を先に）`);
  if (!MODELS) fail('--models（RMVPE・HubertFA の重みのフォルダ）を渡す');
  const entry = JSON.parse(fs.readFileSync(path.join(DIST, 'lyrics-ja.json'), 'utf8'));
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-smoke-addon-'));
  const local = path.join(tmp, 'LocalAppData');
  const addonsDir = path.join(local, 'Gliss', 'addons');
  fs.mkdirSync(local, { recursive: true });
  if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });
  const { server, port, log } = await serve(DIST, RATE);
  const ctx = {
    wav: path.join(tmp, 'synthetic-song.wav'), userData: path.join(tmp, 'userdata'), errors: [],
    env: { ...process.env, LOCALAPPDATA: local, VOCAL_ENGINE_MODELS_DIR: MODELS, GLISS_ADDON_BASE_URL: `http://127.0.0.1:${port}/`,
      VOCAL_EDITOR_MUTE: '1', VOCAL_EDITOR_HIDDEN: '1', VOCAL_EDITOR_IGNORE_MOUSE: '1', ELECTRON_DISABLE_SECURITY_WARNINGS: '1' },
  };
  for (const k of ['ELECTRON_RUN_AS_NODE', 'VOCAL_ENGINE_CWD', 'VOCAL_ENGINE_MODELS', 'PYTHONPATH', 'GLISS_ADDONS_DIR', 'GLISS_ADDON_CATALOG']) delete ctx.env[k];
  synthWav(ctx.wav);
  report.server = `127.0.0.1:${port}`;
  report.addon = { file: entry.file, size: entry.size, sha256: entry.sha256, key: entry.key };
  let cur = null;
  try {
    // ---- 1. アドオン無し
    cur = await launch(ctx);
    let i = await info(cur.win);
    if (i.addons.installed.length) fail('最初からアドオンがある');
    if (i.addons.dir.toLowerCase() !== addonsDir.toLowerCase()) fail(`アドオンの置き場が違う: ${i.addons.dir}`);
    const k0 = await tryKanji(cur.win);
    const kana = await cur.win.evaluate(() => window.api.call('set_lyrics', { text: 'よぞらにうたう', author: 'human' }));
    step('1. アドオン無し', { g2p: i.phonemes.g2p, addonsDir: i.addons.dir, kanji: k0, kanaOk: kana.ok !== false });
    if (k0.ok || !/モデルと追加の機能/.test(JSON.stringify(k0))) fail('アドオン無しで漢字の歌詞が通った／案内が無い');
    await openDialog(cur.win);
    let r = await row(cur.win);
    if (r.state !== 'missing') fail(`行が missing でない: ${r.state}`);
    await shot(cur.win, 'addon-1-missing.png');

    // ---- 2. 画面からダウンロード
    const rowSel = '#addonsDlg [data-addon="lyrics-ja"]';
    await cur.win.locator(`${rowSel} button[data-b="license"]`).click();
    await shot(cur.win, 'addon-2-license.png');
    await cur.win.locator(`${rowSel} button[data-b="license"]`).click();
    const t0 = Date.now();
    await cur.win.locator(`${rowSel} button[data-b="download"]`).click();
    await cur.win.waitForFunction(() => { const p = window.__app.addons().progress; return p.phase === 'downloading' && p.bytes > p.total * 0.3; }, null, { timeout: 60000, polling: 100 });
    await shot(cur.win, 'addon-3-downloading.png');
    r = await waitRow(cur.win, () => window.__app.addons().progress.phase === 'done' && window.__app.addons().addons[0].state === 'ready', 180000);
    const sec = (Date.now() - t0) / 1000;
    await shot(cur.win, 'addon-4-ready.png');
    i = await info(cur.win);
    const k1 = await tryKanji(cur.win);
    step('2. 画面から取得（再起動なし）', { seconds: sec, row: { state: r.state, note: r.note }, installed: i.addons.installed, g2p: i.phonemes.g2p, kanji: k1,
      files: fs.readdirSync(path.join(addonsDir, 'lyrics-ja')) });
    if (!k1.ok) fail(`取得した後も漢字の歌詞が通らない: ${k1.error}`);
    await cur.win.evaluate(() => window.__app.closeAddons());
    // 画面の歌詞の入力と同じ道（renderer の setLyrics）でも入れて、音素の段を描き直させてから撮る
    await cur.win.evaluate((t) => window.__app.setLyrics(t), KANJI);
    await cur.win.waitForFunction(() => window.__app.idle() && window.__app.ready(), null, { timeout: 120000 });
    await shot(cur.win, 'addon-5-kanji-lyrics.png');
    await closeApp(cur.app, cur.win); cur = null;

    // ---- 3. 起動し直す・AI クライアントと同じ exe を単体で
    cur = await launch(ctx);
    i = await info(cur.win);
    const k2 = await tryKanji(cur.win);
    const engineExe = path.join(path.dirname(EXE), 'resources', 'engine', 'vocal-engine', 'vocal-engine.exe');
    const envCheck = { ...ctx.env }; delete envCheck.GLISS_ADDONS_DIR;
    const chk = JSON.parse(execFileSync(engineExe, ['--check'], { env: envCheck, encoding: 'utf8', timeout: 180000, stdio: ['ignore', 'pipe', 'ignore'] }));
    step('3. 起動し直す', { installed: i.addons.installed.map((a) => ({ id: a.id, active: a.active, source: a.source })), kanji: k2,
      standaloneExe: { addons_dir: chk.addons_dir, addons: chk.addons.map((a) => ({ id: a.id, active: a.active })), imports: chk.addon_imports, probe: chk.addon_probe } });
    if (!i.addons.installed[0]?.active || !k2.ok) fail('起動し直したらアドオンを読んでいない');
    if (!chk.addons[0]?.active) fail('単体の exe がアドオンを読まない');

    // ---- 4. 削除（使っているので次の起動で）
    await openDialog(cur.win);
    await cur.win.locator(`${rowSel} button[data-b="remove"]`).click();
    r = await waitRow(cur.win, () => window.__app.addons().addons[0].state === 'removing');
    await shot(cur.win, 'addon-6-removing.png');
    step('4. 削除（使用中）', { state: r.state, mark: fs.existsSync(path.join(addonsDir, 'lyrics-ja', 'gliss-addon.remove')) });
    await closeApp(cur.app, cur.win); cur = null;
    cur = await launch(ctx);
    i = await info(cur.win);
    await openDialog(cur.win);
    r = await row(cur.win);
    step('4b. 起動し直すと消えている', { exists: fs.existsSync(path.join(addonsDir, 'lyrics-ja')), left: fs.readdirSync(addonsDir), state: r.state, installed: i.addons.installed });
    if (fs.existsSync(path.join(addonsDir, 'lyrics-ja')) || r.state !== 'missing') fail('起動し直しても消えていない');
    await closeApp(cur.app, cur.win); cur = null;

    // ---- 5. 互換の無いアドオン（エンジンの numpy と版が違う）
    const stale = path.join(addonsDir, 'lyrics-ja');
    await extract(path.join(DIST, entry.file), { dir: stale });
    const m = JSON.parse(fs.readFileSync(path.join(stale, 'gliss-addon.json'), 'utf8'));
    fs.writeFileSync(path.join(stale, 'gliss-addon.json'), JSON.stringify({ ...m, key: 'stale0000000', requires: { ...m.requires, numpy: '2.3.0' } }));
    cur = await launch(ctx);
    i = await info(cur.win);
    const k3 = await tryKanji(cur.win);
    await openDialog(cur.win);
    r = await row(cur.win);
    await shot(cur.win, 'addon-7-incompatible.png');
    step('5. 互換の無いアドオン', { installed: i.addons.installed, g2p: i.phonemes.g2p, kanji: k3, row: { state: r.state, reason: r.reason } });
    if (r.state !== 'incompatible' || i.addons.installed[0]?.active || k3.ok) fail('互換の無いアドオンを読んだ／案内が出ない');
    await cur.win.locator(`${rowSel} button[data-b="update"]`).click();
    r = await waitRow(cur.win, () => window.__app.addons().progress.phase === 'done' && window.__app.addons().addons[0].state === 'ready', 180000);
    const after = JSON.parse(fs.readFileSync(path.join(stale, 'gliss-addon.json'), 'utf8'));
    i = await info(cur.win);
    step('5b. 更新で取り直す', { state: r.state, key: after.key, installed: i.addons.installed.map((a) => ({ id: a.id, active: a.active })) });
    if (after.key !== entry.key) fail('取り直した版が目録と違う');
    // 読み込む前（漢字をまだ読んでいない）なら、削除はその場で済む
    await cur.win.locator(`${rowSel} button[data-b="remove"]`).click();
    r = await waitRow(cur.win, () => window.__app.addons().addons[0].state === 'missing');
    i = await info(cur.win);
    step('5c. 読み込む前の削除はその場で', { state: r.state, exists: fs.existsSync(stale), installed: i.addons.installed, sysPathDropped: true });
    if (fs.existsSync(stale)) fail('読み込む前なのに消えない');
    await cur.win.locator(`${rowSel} button[data-b="download"]`).click();
    r = await waitRow(cur.win, () => window.__app.addons().progress.phase === 'done' && window.__app.addons().addons[0].state === 'ready', 180000);
    const k4 = await tryKanji(cur.win);
    step('5d. もう一度取得', { state: r.state, kanji: k4 });
    if (!k4.ok) fail('取り直した後に漢字が読めない');
    await closeApp(cur.app, cur.win); cur = null;
    report.httpLog = log;
    report.errors = ctx.errors;
  } finally {
    if (cur) await closeApp(cur.app, cur.win);
    server.close();
    try { fs.rmSync(tmp, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 }); } catch { /* 掴まれていたら残る */ }
  }
}

const watchdog = setTimeout(() => {
  killTree(launchedPid);
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
  console.error(JSON.stringify(report, null, 2));
  console.error(`失敗: ${e.message}`);
  clearTimeout(watchdog);
  killTree(launchedPid);
  process.exit(1);
}
