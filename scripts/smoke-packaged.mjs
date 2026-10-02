// 配布版（electron-builder --dir の展開版、またはインストール済みの Gliss.exe）を起動して確かめる。
//
//   node scripts/smoke-packaged.mjs [--exe <Gliss.exe>] [--models <重みのあるフォルダ>] [--shots <フォルダ>] [--no-analyze] [--open-without-models] [--timeout <ms。既定 180000>]
//
//   既定の exe: app/dist/win-unpacked/Gliss.exe
//   --models:   RMVPE（rmvpe.onnx）と HubertFA（hubertfa/）が入っているフォルダ（開発機の %LOCALAPPDATA%\Gliss\models など）。
//               一時の %LOCALAPPDATA%\Gliss\models にコピーして使い、終わったら消す。
//               付けなければ重みは無い状態で起動する（初回の「モデルの準備」画面と、重み無しの縮退を確かめる）。
//
// 確かめること:
//   1. 起動して、同梱のエンジン exe（resources/engine/vocal-engine/vocal-engine.exe）に MCP で接続できる
//   2. 合成した WAV（実素材は使わない）を開いて解析できる（--models があるとき。ノート数・F0 の値）
//   3. 重みが無いときは落ちずに、画面が「モデルの準備」へ案内する（--open-without-models: その状態で WAV を開いたときの画面）
// 実素材・利用者の %LOCALAPPDATA% には触れない（LOCALAPPDATA を一時フォルダに差し替えて起動する）。
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const { _electron: electron } = createRequire(path.join(ROOT, 'app', 'package.json'))('@playwright/test');

const argv = process.argv.slice(2);
const opt = (name) => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : null; };
const EXE = path.resolve(opt('--exe') || path.join(ROOT, 'app', 'dist', 'win-unpacked', 'Gliss.exe'));
const MODELS = opt('--models') ? path.resolve(opt('--models')) : null;
const SHOTS = opt('--shots') ? path.resolve(opt('--shots')) : null;

/** 合成の歌（A3→C4→E4。ビブラート付きの倍音列、音の間に無音）を 16 bit モノラルの WAV に。 */
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

function engineProcesses() {
  const out = execFileSync('powershell.exe', ['-NoProfile', '-Command',
    "Get-CimInstance Win32_Process -Filter \"Name='vocal-engine.exe'\" | ForEach-Object { $_.ExecutablePath }"],
  { encoding: 'utf8' });
  return out.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
}

const report = {};
const fail = (m) => { throw new Error(m); };
const TIMEOUT_MS = Number(opt('--timeout') || 180000);
let launchedPid = null;   // 時間切れのとき木ごと落とす

/** pid を子ごと強制終了する（残ったエンジン exe がポートやファイルを掴まないように）。 */
function killTree(pid) {
  if (!pid) return;
  try { execFileSync('taskkill.exe', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' }); } catch { /* もう無い */ }
}

/** 閉じる。「無題*」のまま閉じると「保存しますか」のダイアログで止まるので、先に保存済みに戻す。
 * それでも 15 秒で閉じなければ木ごと強制終了する（スモークがハングして後に残さない）。 */
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
}

async function main() {
  if (!fs.existsSync(EXE)) fail(`${EXE} が無い（pnpm dist:dir を先に）`);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-smoke-'));
  const local = path.join(tmp, 'LocalAppData');
  fs.mkdirSync(path.join(local, 'Gliss', 'models'), { recursive: true });
  if (MODELS) fs.cpSync(MODELS, path.join(local, 'Gliss', 'models'), { recursive: true });
  const wav = path.join(tmp, 'synthetic-song.wav');
  synthWav(wav);
  const env = { ...process.env, LOCALAPPDATA: local, VOCAL_EDITOR_MUTE: '1', VOCAL_EDITOR_HIDDEN: '1',
    VOCAL_EDITOR_IGNORE_MOUSE: '1', ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  for (const k of ['ELECTRON_RUN_AS_NODE', 'VOCAL_ENGINE_CWD', 'VOCAL_ENGINE_MODELS_DIR', 'VOCAL_ENGINE_MODELS', 'PYTHONPATH']) delete env[k];
  const args = ['--user-data-dir', path.join(tmp, 'userdata'), '--mute'];
  if ((MODELS && !argv.includes('--no-analyze')) || argv.includes('--open-without-models')) args.push('--take', wav);
  const app = await electron.launch({ executablePath: EXE, args, env });
  launchedPid = app.process().pid;
  let win = null;
  try {
    win = await app.firstWindow();
    const errors = [];
    win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
    win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
    report.main = await app.evaluate(({ app: a }) => ({ isPackaged: a.isPackaged, version: a.getVersion(), name: a.getName(),
      resources: process.resourcesPath, userData: a.getPath('userData'), exe: process.execPath }));
    if (!report.main.isPackaged) fail('app.isPackaged が false（配布版として起動していない）');
    await win.waitForFunction(() => !!window.api && !!window.__app, null, { timeout: 120000 });
    const boot = await win.evaluate(() => window.api.bootstrap());
    report.engineReady = boot.engineReady; report.engineError = boot.engineError;
    if (!boot.engineReady) fail(`エンジンに接続できない:\n${boot.engineError}`);
    const info = await win.evaluate(() => window.api.call('engine_info', {}));
    report.engine = { version: info.version, rmvpe_model: info.rmvpe_model, rmvpe_model_found: info.rmvpe_model_found, log: info.log };
    report.engineProcesses = engineProcesses();
    const expectExe = path.join(report.main.resources, 'engine', 'vocal-engine', 'vocal-engine.exe').toLowerCase();
    if (!report.engineProcesses.some((p) => p.toLowerCase() === expectExe)) fail(`同梱のエンジン exe が動いていない: ${expectExe}`);
    const wantModels = path.join(local, 'Gliss', 'models', 'rmvpe.onnx').toLowerCase();
    if (info.rmvpe_model.toLowerCase() !== wantModels) fail(`重みの置き場が想定と違う: ${info.rmvpe_model}`);
    if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });

    if (MODELS) {
      if (!info.rmvpe_model_found) fail('重みをコピーしたのにエンジンが見つけない');
      if (!argv.includes('--no-analyze')) {
        await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
        const notes = await win.evaluate(() => window.__app.notes());
        report.analyze = { notes: notes.length, keys: Object.keys(notes[0] || {}), first: notes.slice(0, 3) };
        if (!notes.length) fail('解析してもノートが 0 個');
        if (SHOTS) await win.screenshot({ path: path.join(SHOTS, 'smoke-analyzed.png') });
      }
    } else {
      // 重み無し: 「モデルの準備」画面が出る。ここで WAV を開いても落ちず、理由が出る
      const open = argv.includes('--open-without-models');
      if (!open) {
        await win.locator('#firstRun').waitFor({ state: 'visible', timeout: 30000 });
        report.firstRunVisible = true;
        report.firstRunText = (await win.locator('#firstRun').innerText()).slice(0, 400);
      }
      if (open) {
        // 重み無しのまま WAV を開く: 落ちずに、理由が出て「モデルの準備」が残る
        await win.waitForTimeout(8000);
        report.afterOpen = await win.evaluate(() => ({ body: document.body.innerText.slice(0, 600),
          firstRunHidden: document.querySelector('#firstRun')?.hidden, status: document.querySelector('#status')?.innerText }));
      }
      if (SHOTS) await win.screenshot({ path: path.join(SHOTS, open ? 'smoke-open-without-models.png' : 'smoke-first-run.png') });
    }
    report.errors = errors;
  } finally {
    await closeApp(app, win);
    try { fs.rmSync(tmp, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 }); } catch { /* 掴まれていたら一時フォルダに残る */ }
  }
}

// 全体の時間切れ: 何かが止まっても終わらせる（残ったアプリは main の closeApp が落とす。ここは最後の砦）
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
} catch (e) {
  console.error(JSON.stringify(report, null, 2));
  console.error(`失敗: ${e.message}`);
  process.exit(1);
}
