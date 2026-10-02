// リリースの前に、版・タグ・リリースノートがそろっているかをまとめて確かめる（CI の release.yml が最初に呼ぶ）。
//
//   node scripts/release-check.mjs                 版の形・engine 側の写し・releases/<版>.json
//   node scripts/release-check.mjs --tag v0.1.0-beta.1   加えて、タグが v<app/package.json の版> であること
//
// 確かめること:
//   - app/package.json の version が 0.1.0 か 0.1.0-beta.N の形（docs/release-plan.md §2）
//   - タグ（--tag）が v<version> と一致する
//   - engine 側の版（pyproject.toml・__version__）が写されている（scripts/sync-version.mjs）
//   - releases/<version>.json があり、形が正しい（すべての正本も）
//   - 直前の版のノートと題・中身が同じではない（版だけ上げてノートを写したまま出さない）
// タグが main の上にあることはワークフロー側（git merge-base）で見る。
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { RELEASE_VERSION, appVersion, readReleases } from './release-notes.mjs';
import { staleFiles } from './sync-version.mjs';

export const TAG = /^v(\d+\.\d+\.\d+(?:-beta\.\d+)?)$/;

/** 問題の文の配列（無ければ空）。読み込みは引数で差し替えられる（テスト用）。 */
export function releaseCheck({ tag = null, version = appVersion(), releases = null, stale = null } = {}) {
  const out = [];
  if (!RELEASE_VERSION.test(version)) {
    out.push(`app/package.json の version が 0.1.0 か 0.1.0-beta.N の形ではない: ${version}`);
    return out;
  }
  if (tag !== null) {
    if (!TAG.test(tag)) out.push(`タグの形が違う（v0.1.0 か v0.1.0-beta.N）: ${tag}`);
    else if (tag !== `v${version}`) out.push(`タグ ${tag} と app/package.json の版 ${version} が違う`);
  }
  try {
    const s = stale ?? staleFiles(version);
    for (const f of s) out.push(`engine 側の版がずれている: ${f}（node scripts/sync-version.mjs で写す）`);
  } catch (e) {
    out.push(e.message);
  }
  let list;
  try {
    list = releases ?? readReleases();
  } catch (e) {
    out.push(e.message);
    return out;
  }
  const i = list.findIndex((r) => r.version === version);
  if (i < 0) {
    out.push(`releases/${version}.json が無い`);
    return out;
  }
  const prev = list[i + 1];
  const notes = (r) => JSON.stringify([r.title, r.sections]);
  if (prev && notes(prev) === notes(list[i])) {
    out.push(`releases/${version}.json のノートが直前の ${prev.version} と同じ`);
  }
  return out;
}

function main() {
  const i = process.argv.indexOf('--tag');
  const tag = i > 0 ? (process.argv[i + 1] ?? '') : null;
  const problems = releaseCheck({ tag });
  if (problems.length) {
    console.error(`リリースの検査に通らなかった:\n  ${problems.join('\n  ')}`);
    process.exit(1);
  }
  console.log(`リリースの検査を通った: ${appVersion()}${tag ? `（タグ ${tag}）` : ''}`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
