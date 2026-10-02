// エンジン exe（PyInstaller のフォルダ形式。engine/packaging/vocal-engine.spec）を再現できる形で作る。
//
//   node scripts/build-engine.mjs            .venv-exe（Python 3.13）を作って（あれば再利用）依存を入れ、固めて、--check で確かめる
//   node scripts/build-engine.mjs --clean    .venv-exe を作り直してから
//
// 出力: engine/packaging/dist/vocal-engine/vocal-engine.exe（electron-builder が resources/engine/ に同梱する。
//       app/electron-builder.yml の extraResources）。
// 前提: uv（https://docs.astral.sh/uv/）が PATH にあること。Python 3.13 は uv が用意する。
// 依存は engine/packaging/requirements-exe.txt（ハッシュ付きの固定。作り直しは requirements-exe.in の冒頭の手順）。
// **torch は入れない**（CUDA 版を含む）。VC++ ランタイムは spec が System32 のものに差し替える。
// GitHub Actions の windows-latest からも同じ手順で呼ぶ。
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PACKAGING = path.join(ROOT, 'engine', 'packaging');
const VENV = process.env.GLISS_EXE_VENV ? path.resolve(process.env.GLISS_EXE_VENV) : path.join(ROOT, '.venv-exe');
const PYTHON_VERSION = '3.13';
const WIN = process.platform === 'win32';
const VENV_PYTHON = path.join(VENV, WIN ? 'Scripts' : 'bin', WIN ? 'python.exe' : 'python');
const DIST = path.join(PACKAGING, 'dist');
const EXE = path.join(DIST, 'vocal-engine', WIN ? 'vocal-engine.exe' : 'vocal-engine');

function run(cmd, args, opt = {}) {
  console.log(`> ${cmd} ${args.join(' ')}`);
  const r = spawnSync(cmd, args, { stdio: 'inherit', cwd: PACKAGING, ...opt });
  if (r.error) throw new Error(`${cmd} を起動できない: ${r.error.message}（uv が PATH にあるか確認する）`);
  if (r.status !== 0) throw new Error(`${cmd} が終了コード ${r.status} で失敗した`);
}

function dirStats(dir) {
  let bytes = 0; let files = 0;
  for (const e of fs.readdirSync(dir, { withFileTypes: true, recursive: true })) {
    if (!e.isFile()) continue;
    files += 1;
    bytes += fs.statSync(path.join(e.parentPath, e.name)).size;
  }
  return { bytes, files };
}

function main() {
  if (process.argv.includes('--clean')) fs.rmSync(VENV, { recursive: true, force: true });
  if (!fs.existsSync(VENV_PYTHON)) run('uv', ['venv', VENV, '--python', PYTHON_VERSION]);
  const ver = spawnSync(VENV_PYTHON, ['-c', 'import sys; print("%d.%d" % sys.version_info[:2])'], { encoding: 'utf8' });
  if (ver.stdout.trim() !== PYTHON_VERSION) {
    throw new Error(`${VENV} の Python が ${PYTHON_VERSION} ではない（${ver.stdout.trim()}）。--clean で作り直す`);
  }
  run('uv', ['pip', 'install', '--python', VENV_PYTHON, '--require-hashes', '--no-deps',
    '-r', path.join(PACKAGING, 'requirements-exe.txt')]);

  fs.rmSync(path.join(PACKAGING, 'build'), { recursive: true, force: true });
  fs.rmSync(DIST, { recursive: true, force: true });
  run(VENV_PYTHON, ['-m', 'PyInstaller', 'vocal-engine.spec', '--noconfirm',
    '--distpath', DIST, '--workpath', path.join(PACKAGING, 'build')]);

  // 起動できること・torch を引き込んでいないこと
  console.log(`> ${EXE} --check`);
  const chk = spawnSync(EXE, ['--check'], { encoding: 'utf8', maxBuffer: 16 << 20, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
  if (chk.status !== 0) throw new Error(`--check が終了コード ${chk.status}:\n${chk.stderr}`);
  const info = JSON.parse(chk.stdout);
  const errors = Object.entries(info).filter(([, v]) => typeof v === 'string' && v.startsWith('ERROR:'));
  if (errors.length) throw new Error(`import に失敗した: ${errors.map(([k, v]) => `${k} ${v}`).join(', ')}`);
  if (info.torch_loaded || info.torch_importable) throw new Error('torch が入っている（入れない方針）');
  if (!info.frozen) throw new Error('--check が固めたものとして動いていない');
  const { bytes, files } = dirStats(path.join(DIST, 'vocal-engine'));
  console.log(`できた: ${EXE}`);
  console.log(`  ${(bytes / 1048576).toFixed(0)} MB・${files} ファイル・import ${info.import_sec} 秒・backends ${JSON.stringify(info.backends)}`);
}

try { main(); } catch (e) { console.error(`失敗: ${e.message}`); process.exit(1); }
