// インストール先のエンジン exe（resources/engine/vocal-engine/vocal-engine.exe）で動いているプロセスを探す・止める。
// 再起動して更新の前に使う（main.mjs）。Electron に依存しない。
//
// 画面が起動したエンジンだけでなく、Claude Code / Claude Desktop に登録したエンジン（ai-connect.mjs が
// 同じ exe の絶対パスを登録する）も同じ exe で動く。動いたままだと exe と _internal の DLL が掴まれていて、
// インストーラが上書きできない（Windows は実行中のファイルを置き換えられない）。
// インストーラ側（app/build/installer.nsh の customCheckAppRunning と、electron-builder 26 の NSIS の既定の
// CHECK_APP_RUNNING）もインストール先の下で動くエンジンを止める（app/electron-builder.yml の nsis の説明）。
// こちらはその前に、利用者に知らせてから止めるためのもの。
import { execFile } from 'node:child_process';
import path from 'node:path';
import { promisify } from 'node:util';

const run = promisify(execFile);
export const ENGINE_EXE = 'vocal-engine.exe';

// 探す場所は環境変数で渡す（パスに ' や日本語が入っても、コマンドの文字列に埋め込まない）
const LIST = [
  "$d = $env:GLISS_ENGINE_DIR",
  "Get-CimInstance Win32_Process -Filter \"Name='vocal-engine.exe'\" |",
  "  Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($d, [StringComparison]::OrdinalIgnoreCase) } |",
  "  ForEach-Object { $_.ProcessId }",
].join('\n');

function powershell(script, env) {
  const exe = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  return run(exe, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
    { env: { ...process.env, ...env }, windowsHide: true, timeout: 30000, encoding: 'utf8' });
}

/** dir（エンジン exe のフォルダ）の下の vocal-engine.exe のプロセス ID。Windows 以外は空。 */
export async function findEngineProcesses(dir, { exec = powershell, platform = process.platform } = {}) {
  if (platform !== 'win32' || !dir) return [];
  const prefix = path.resolve(dir).replace(/[\\/]*$/, path.sep);
  const { stdout } = await exec(LIST, { GLISS_ENGINE_DIR: prefix });
  return String(stdout).split(/\r?\n/).map((s) => Number(s.trim())).filter((n) => Number.isInteger(n) && n > 0);
}

/**
 * dir の下の vocal-engine.exe をすべて止め、消えるまで待つ（最長 timeoutMs）。
 * @returns {Promise<{stopped: number[], left: number[]}>}
 */
export async function stopEngineProcesses(dir, { find = findEngineProcesses, kill = killTree, sleep = (ms) => new Promise((r) => setTimeout(r, ms)), timeoutMs = 10000 } = {}) {
  const first = await find(dir);
  for (const pid of first) await kill(pid).catch(() => { /* もう終わっていた */ });
  let left = first;
  const until = Date.now() + timeoutMs;
  while (left.length && Date.now() < until) {
    await sleep(250);
    left = await find(dir);
  }
  return { stopped: first.filter((p) => !left.includes(p)), left };
}

function killTree(pid) {
  const exe = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'taskkill.exe');
  return run(exe, ['/PID', String(pid), '/T', '/F'], { windowsHide: true, timeout: 15000 });
}
