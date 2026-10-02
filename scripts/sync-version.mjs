// 版の正本は app/package.json の version 1 か所。エンジン側（engine/pyproject.toml・
// engine/vocal_engine/__init__.py）はここから書き写す。
//
//   node scripts/sync-version.mjs          書き写す（変わったファイルを表示）
//   node scripts/sync-version.mjs --check  ずれていたら終了コード 1（CI・テスト用。ファイルは書かない）
//
// 版は SemVer（`0.1.0`、先行版は `0.1.0-beta.1`）。pyproject.toml は PEP 440 でなければならないので
// `0.1.0-beta.1` → `0.1.0b1` に直して書く（`__version__` は SemVer のまま）。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PACKAGE_JSON = path.join(ROOT, 'app', 'package.json');
const PYPROJECT = path.join(ROOT, 'engine', 'pyproject.toml');
const INIT_PY = path.join(ROOT, 'engine', 'vocal_engine', '__init__.py');

const SEMVER = /^(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?$/;

/** SemVer → PEP 440（`0.1.0-beta.2` → `0.1.0b2`、`1.0.0-rc.1` → `1.0.0rc1`）。 */
export function toPep440(version) {
  const m = SEMVER.exec(version);
  if (!m) throw new Error(`版は 0.1.0 か 0.1.0-beta.1 の形で書く（alpha / beta / rc）: ${version}`);
  const [, major, minor, patch, pre, n] = m;
  const tag = { alpha: 'a', beta: 'b', rc: 'rc' }[pre];
  return `${major}.${minor}.${patch}${pre ? `${tag}${n}` : ''}`;
}

export function sourceVersion() {
  return JSON.parse(fs.readFileSync(PACKAGE_JSON, 'utf8')).version;
}

const PYPROJECT_LINE = /^version = "[^"]*"$/m;
const INIT_LINE = /^__version__ = "[^"]*"$/m;

/** 期待する中身に直したテキスト（変わらなければ同じ文字列）。 */
function rewrite(file, text, version) {
  const [re, line] = file === PYPROJECT
    ? [PYPROJECT_LINE, `version = "${toPep440(version)}"`]
    : [INIT_LINE, `__version__ = "${version}"`];
  if (!re.test(text)) throw new Error(`${file} に版の行が見つからない`);
  return text.replace(re, line);
}

/** 版がずれているファイル（ROOT からの相対パス）。書き換えはしない（release-check.mjs も使う）。 */
export function staleFiles(version = sourceVersion()) {
  toPep440(version);   // 形の検査
  return [PYPROJECT, INIT_PY]
    .filter((file) => {
      const text = fs.readFileSync(file, 'utf8');
      return rewrite(file, text, version) !== text;
    })
    .map((file) => path.relative(ROOT, file));
}

function main() {
  const check = process.argv.includes('--check');
  const version = sourceVersion();
  const stale = staleFiles(version);
  if (check) {
    for (const f of stale) console.error(`版がずれている: ${f}（app/package.json は ${version}）`);
    if (stale.length) process.exit(1);
  } else {
    for (const f of stale) {
      const file = path.join(ROOT, f);
      fs.writeFileSync(file, rewrite(file, fs.readFileSync(file, 'utf8'), version), 'utf8');
      console.log(`書き換えた: ${f} → ${version}`);
    }
  }
  if (!stale.length) console.log(`版は ${version} でそろっている`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
