// そろえた成果物で GitHub Release を作って公開する（CI の release.yml。gh を使う）。
//
//   node scripts/release-publish.mjs --tag v0.1.0-beta.1 --assets <フォルダ> --notes <Markdown>
//   環境変数: GH_TOKEN（contents: write）・GH_REPO（owner/repo）
//
// - 同じタグの Release が既にあれば止める（一度配布した版のファイルは上書きしない。直すときは版を上げる）
// - 下書き（draft）で作って全部のファイルを載せてから公開する（載せている途中の Release を自動更新が見ないように。
//   下書きは releases.atom にも /releases/latest にも出ない）
// - beta（v0.1.0-beta.N）は prerelease にして「Latest」にしない。正式版は「Latest」にする（stable の人は /releases/latest を読む）
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { TAG } from './release-check.mjs';

/** gh release create の引数（テスト用に切り出す）。 */
export function createArgs({ tag, files, notes }) {
  const beta = /-beta\.\d+$/.test(tag);
  return ['release', 'create', tag, ...files, '--verify-tag', '--draft', '--title', `Gliss ${tag.slice(1)}`,
    '--notes-file', notes, ...(beta ? ['--prerelease'] : [])];
}

export function publishArgs({ tag }) {
  const beta = /-beta\.\d+$/.test(tag);
  return ['release', 'edit', tag, '--draft=false', `--latest=${beta ? 'false' : 'true'}`];
}

function main() {
  const argv = process.argv.slice(2);
  const opt = (k) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : null; };
  const tag = opt('--tag');
  const assets = opt('--assets') && path.resolve(opt('--assets'));
  const notes = opt('--notes') && path.resolve(opt('--notes'));
  if (!TAG.test(tag || '')) throw new Error(`タグの形が違う: ${tag}`);
  if (!/^[\w.-]+\/[\w.-]+$/.test(process.env.GH_REPO || '') || !process.env.GH_TOKEN) throw new Error('GH_REPO と GH_TOKEN が要る');
  if (!assets || !fs.existsSync(assets) || !notes || !fs.existsSync(notes)) throw new Error('--assets と --notes が要る');
  const gh = (args) => execFileSync('gh', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  let exists = true;
  try { gh(['release', 'view', tag, '--json', 'tagName']); } catch (e) {
    if (!/release not found/i.test(String(e.stderr))) throw e;
    exists = false;
  }
  if (exists) throw new Error(`${tag} の Release は既にある（上書きしない。直すときは版を上げる）`);
  const files = fs.readdirSync(assets).sort().map((n) => path.join(assets, n));
  console.log(`下書きを作る: ${tag}（${files.length} ファイル）`);
  gh(createArgs({ tag, files, notes }));
  console.log('公開する');
  gh(publishArgs({ tag }));
  console.log(gh(['release', 'view', tag, '--json', 'url,isPrerelease,isDraft,assets', '--jq', '{url, isPrerelease, isDraft, assets: [.assets[].name]}']));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main(); } catch (e) { console.error(`失敗: ${e.message}${e.stderr ? `\n${e.stderr}` : ''}`); process.exit(1); }
}
