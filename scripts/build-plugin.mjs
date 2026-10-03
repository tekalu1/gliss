// DAW のプラグイン（VST3 + ARA 2。plugin/）を配布用に作る。docs/ara-plugin.md の「配布」。
//
//   node scripts/build-plugin.mjs                 cmake の構成（初回は依存を取る）→ Release のビルド → plugin/build/dist に写す
//   node scripts/build-plugin.mjs --check         plugin/build/dist に Gliss.vst3 があるかだけ確かめる（pnpm dist:dir の前）
//   node scripts/build-plugin.mjs -- <cmake の引数>  構成に足す（手元で取得済みの依存を使う -DFETCHCONTENT_SOURCE_DIR_JUCE=… など）
//
// 出力: plugin/build/dist/Gliss.vst3（electron-builder が resources/plugin/Gliss.vst3 に同梱する。app/electron-builder.yml の
//       extraResources。インストーラが %LOCALAPPDATA%\Programs\Common\VST3 に写す。app/build/installer.nsh）と、
//       plugin/build/dist/gliss-plugin.json（plugin/CMakeLists.txt の末尾が書くビルドの情報。依存の置き場と版。
//       scripts/third-party-notices.mjs が依存のライセンス文を集めるのに使う）。
//       Gliss.vst3 の Contents/Resources に、ライセンスの文書（NOTICE.txt・LICENSE-AGPL-3.0.txt・LICENSE-GPL-3.0.txt・
//       THIRD_PARTY_NOTICES.txt）を足す（Gliss.vst3 だけを別の場所へ写しても、ライセンスの文書が付いていくように）。
// 前提: CMake 3.22 以上と、Visual Studio（2022 以降）の C++ のデスクトップ開発（MSVC x64）。管理者権限は要らない。
// 構成は plugin/build（plugin/scripts/test-plugin.ps1 と同じ）。既に構成してあれば、その generator のまま使う。
// GitHub Actions の windows-latest からも同じ手順で呼ぶ（.github/workflows/release.yml）。
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { PLUGIN_DIST, pluginLicenseLines, pluginPackages, readPluginBuild, section } from './third-party-notices.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PLUGIN = path.join(ROOT, 'plugin');
const BUILD = path.join(PLUGIN, 'build');
const CONFIG = 'Release';
const BUNDLE = path.join(PLUGIN_DIST, 'Gliss.vst3');
const BINARY = path.join(BUNDLE, 'Contents', 'x86_64-win', 'Gliss.vst3');
// Visual Studio の版（vswhere の installationVersion の上の桁）→ CMake の generator
const GENERATORS = { 16: 'Visual Studio 16 2019', 17: 'Visual Studio 17 2022', 18: 'Visual Studio 18 2026' };

function run(cmd, args) {
  console.log(`> ${cmd} ${args.join(' ')}`);
  // MSBuild の常駐ノードを残さない（ビルドの後にプロセスが残り、worktree を掴む）
  const r = spawnSync(cmd, args, { stdio: 'inherit', env: { ...process.env, MSBUILDDISABLENODEREUSE: '1' } });
  if (r.error) throw new Error(`${cmd} を起動できない: ${r.error.message}`);
  if (r.status !== 0) throw new Error(`${cmd} が終了コード ${r.status} で失敗した`);
}

/** cmake が使えるか（版を返す）。 */
function cmakeVersion() {
  const r = spawnSync('cmake', ['--version'], { encoding: 'utf8' });
  if (r.error || r.status !== 0) throw new Error('cmake が見つからない（CMake 3.22 以上を入れて PATH に通す）');
  return /cmake version (\S+)/.exec(r.stdout)?.[1] || '?';
}

/** MSVC（x64）の入った Visual Studio。{ name, version, generator } */
function visualStudio() {
  const vswhere = path.join(process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'Microsoft Visual Studio', 'Installer', 'vswhere.exe');
  if (!fs.existsSync(vswhere)) throw new Error(`Visual Studio が見つからない（${vswhere} が無い。C++ のデスクトップ開発を入れる）`);
  const r = spawnSync(vswhere, ['-latest', '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
    '-format', 'json', '-utf8'], { encoding: 'utf8' });
  const vs = JSON.parse(r.stdout || '[]')[0];
  if (!vs) throw new Error('MSVC（x64）の入った Visual Studio が無い（C++ のデスクトップ開発を入れる）');
  const major = Number(vs.installationVersion.split('.')[0]);
  return { name: vs.displayName, version: vs.installationVersion, generator: GENERATORS[major] || null };
}

/** 構成済みの generator（plugin/build/CMakeCache.txt）。無ければ null。 */
function cachedGenerator() {
  const cache = path.join(BUILD, 'CMakeCache.txt');
  if (!fs.existsSync(cache)) return null;
  return /^CMAKE_GENERATOR:INTERNAL=(.*)$/m.exec(fs.readFileSync(cache, 'utf8'))?.[1]?.trim() || null;
}

/** ライセンスの文書を Gliss.vst3 の Contents/Resources に置く。 */
function writeLicenses(info, version) {
  const res = path.join(BUNDLE, 'Contents', 'Resources');
  fs.mkdirSync(res, { recursive: true });
  const agpl = path.join(info.juce.dir, 'modules', 'juce_gui_extra', 'native', 'typescript', 'webview-interop', 'LICENSE-AGPL.md');
  fs.copyFileSync(agpl, path.join(res, 'LICENSE-AGPL-3.0.txt'));
  fs.copyFileSync(path.join(ROOT, 'LICENSE'), path.join(res, 'LICENSE-GPL-3.0.txt'));
  const notice = [
    `Gliss ${version} - DAW plug-in (VST3 + ARA 2)`,
    'Copyright 2026 tekalu',
    '',
    `This plug-in combines the Gliss source code (GPL-3.0-or-later) with JUCE ${info.juce.version}, which Gliss uses`,
    'under the GNU Affero General Public License version 3. As permitted by section 13 of the GNU GPL v3 and of the',
    'GNU AGPL v3, the combined work (this Gliss.vst3) is distributed under the terms of the GNU AGPL v3',
    '(LICENSE-AGPL-3.0.txt). The Gliss source code itself remains licensed under GPL-3.0-or-later (LICENSE-GPL-3.0.txt).',
    '',
    'This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied',
    'warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the licenses for more details.',
    '',
    'Additional terms under section 7(e) of the GNU GPL / AGPL: the name "Gliss" and the Gliss logos and icons are not',
    'licensed under these licenses, and no rights under trademark law are granted for them. If you distribute a modified',
    'version, use a different name and logo.',
    '',
    `Corresponding source: https://github.com/tekalu1/gliss (tag v${version}, plugin/), together with the dependencies`,
    'that plugin/CMakeLists.txt and Signalsmith Stretch\'s CMakeLists.txt fetch at pinned versions:',
    `  JUCE ${info.juce.version}            https://github.com/juce-framework/JUCE`,
    `  ARA SDK ${info.araSdk.version.replace(/^releases\//, '')}         https://github.com/Celemony/ARA_SDK`,
    `  Microsoft.Web.WebView2 ${info.webview2.version}  https://www.nuget.org/packages/Microsoft.Web.WebView2`,
    `  Signalsmith Stretch ${info.signalsmithStretch.version}  https://github.com/Signalsmith-Audio/signalsmith-stretch`,
    `  Signalsmith Linear ${info.signalsmithLinear.version}  https://github.com/Signalsmith-Audio/linear`,
    '',
    'Third-party software in this plug-in and its licenses: THIRD_PARTY_NOTICES.txt.',
    '',
  ].join('\n');
  fs.writeFileSync(path.join(res, 'NOTICE.txt'), notice, 'utf8');
  const packages = pluginPackages(info);
  fs.writeFileSync(path.join(res, 'THIRD_PARTY_NOTICES.txt'), [
    `Gliss ${version} の DAW のプラグイン（Gliss.vst3）に含まれるサードパーティのソフトウェアのライセンス`,
    '',
    section('Gliss.vst3', packages, pluginLicenseLines(info)),
  ].join('\n'), 'utf8');
  return packages;
}

/** plugin/build/dist の Gliss.vst3 を確かめる（DLL が VST3 の入口 GetPluginFactory を持つ・ライセンスの文書がある）。 */
function check() {
  if (!fs.existsSync(BINARY)) throw new Error(`${path.relative(ROOT, BINARY)} が無い（node scripts/build-plugin.mjs で作る）`);
  const dll = fs.readFileSync(BINARY);
  if (dll.readUInt16LE(0) !== 0x5a4d || !dll.includes('GetPluginFactory')) throw new Error(`${path.relative(ROOT, BINARY)} が VST3 の DLL に見えない`);
  for (const f of ['moduleinfo.json', 'NOTICE.txt', 'LICENSE-AGPL-3.0.txt', 'LICENSE-GPL-3.0.txt', 'THIRD_PARTY_NOTICES.txt']) {
    if (!fs.existsSync(path.join(BUNDLE, 'Contents', 'Resources', f))) throw new Error(`Gliss.vst3/Contents/Resources/${f} が無い（node scripts/build-plugin.mjs で作り直す）`);
  }
  const info = readPluginBuild();
  return { info, bytes: dll.length };
}

function main() {
  const argv = process.argv.slice(2);
  if (argv.includes('--check')) {
    const { info, bytes } = check();
    console.log(`ある: ${path.relative(ROOT, BUNDLE)}（${(bytes / 1048576).toFixed(1)} MB・プラグイン ${info.version}・JUCE ${info.juce.version}）`);
    return;
  }
  const extra = argv.includes('--') ? argv.slice(argv.indexOf('--') + 1) : [];
  if (process.platform !== 'win32') throw new Error('Windows でだけ作れる（VST3 の x86_64-win）');
  const version = JSON.parse(fs.readFileSync(path.join(ROOT, 'app', 'package.json'), 'utf8')).version;
  console.log(`CMake ${cmakeVersion()}`);
  const vs = visualStudio();
  console.log(`${vs.name} ${vs.version}`);

  const cached = cachedGenerator();
  const configure = ['-S', PLUGIN, '-B', BUILD, '-Wno-dev'];
  if (!cached) {
    if (vs.generator) configure.push('-G', vs.generator);
    configure.push('-A', 'x64');
  } else {
    console.log(`構成済みの generator を使う: ${cached}`);
  }
  run('cmake', [...configure, ...extra]);
  run('cmake', ['--build', BUILD, '--config', CONFIG, '--target', 'GlissARA_VST3', '--parallel']);

  const info = readPluginBuild(path.join(BUILD, `gliss-plugin-${CONFIG}.json`));
  // binary は Gliss.vst3/Contents/x86_64-win/Gliss.vst3（DLL）。その 3 つ上が Gliss.vst3（フォルダ）
  const built = path.resolve(info.binary);
  const bundle = path.dirname(path.dirname(path.dirname(built)));
  if (!fs.existsSync(built) || path.basename(bundle) !== 'Gliss.vst3') throw new Error(`ビルドしたのに ${built} が無い`);
  fs.rmSync(PLUGIN_DIST, { recursive: true, force: true });
  fs.mkdirSync(PLUGIN_DIST, { recursive: true });
  fs.cpSync(bundle, BUNDLE, { recursive: true });
  fs.writeFileSync(path.join(PLUGIN_DIST, 'gliss-plugin.json'), `${JSON.stringify(info, null, 2)}\n`, 'utf8');
  const packages = writeLicenses(info, version);
  const { bytes } = check();
  console.log(`できた: ${path.relative(ROOT, BUNDLE)}（${(bytes / 1048576).toFixed(1)} MB・依存のライセンス ${packages.length} 件）`);
}

try { main(); } catch (e) { console.error(`失敗: ${e.message}`); process.exit(1); }
