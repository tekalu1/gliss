// エンジン exe に同梱した copyleft のパッケージ（GPL・LGPL・MPL）の「対応するソース」（PyPI の sdist）を取ってくる。
// Release に添付する（.github/workflows/release.yml。docs/release-plan.md §3）。
//
//   node scripts/gpl-sources.mjs --out <フォルダ>
//
// どれが copyleft かは、エンジン exe を作った venv（.venv-exe）のメタデータで決める（third-party-notices.mjs の isCopyleft）。
// 2026-10 時点では praat-parselmouth（GPL-3.0-or-later。Praat のソースを含む）・soxr（LGPL-2.1-or-later。libsoxr を含む）・
// certifi（MPL-2.0）。版は venv に入っているもの（= engine/packaging/requirements-exe.txt の固定）と同じ。
// PyPI の JSON API の SHA-256 と照らしてから置く。Gliss 自身のソースは、タグの「Source code」として GitHub が付ける。
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { pythonPackages } from './third-party-notices.mjs';

async function fetchOk(url) {
  const r = await fetch(url, { headers: { 'user-agent': 'gliss-release' } });
  if (!r.ok) throw new Error(`${url} が ${r.status}`);
  return r;
}

/** PyPI の sdist（name==version）の URL・ファイル名・SHA-256。 */
export async function sdistOf(name, version, fetcher = fetchOk) {
  const info = await (await fetcher(`https://pypi.org/pypi/${encodeURIComponent(name)}/${encodeURIComponent(version)}/json`)).json();
  const sdist = (info.urls || []).find((u) => u.packagetype === 'sdist');
  if (!sdist) throw new Error(`${name} ${version} の sdist が PyPI に無い`);
  return { url: sdist.url, filename: sdist.filename, sha256: sdist.digests.sha256 };
}

async function main() {
  const i = process.argv.indexOf('--out');
  if (i < 0 || !process.argv[i + 1]) throw new Error('--out <フォルダ> を付ける');
  const out = path.resolve(process.argv[i + 1]);
  fs.mkdirSync(out, { recursive: true });
  const list = pythonPackages().filter((p) => p.copyleft);
  if (!list.length) throw new Error('copyleft のパッケージが見つからない（.venv-exe を確かめる）');
  for (const p of list) {
    const s = await sdistOf(p.name, p.version);
    const body = Buffer.from(await (await fetchOk(s.url)).arrayBuffer());
    const got = crypto.createHash('sha256').update(body).digest('hex');
    if (got !== s.sha256) throw new Error(`${s.filename} の SHA-256 が PyPI と違う（${got}）`);
    fs.writeFileSync(path.join(out, s.filename), body);
    console.log(`取得した: ${s.filename}（${p.license}、${(body.length / 1048576).toFixed(1)} MB）`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { await main(); } catch (e) { console.error(`失敗: ${e.message}`); process.exit(1); }
}
