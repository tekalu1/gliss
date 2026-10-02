// 起動引数の解釈。main.mjs から使う。Electron に依存しない（テストから直接呼べる）。
//
// `--take <wav>` などのオプションのほかに、**オプションでない最初の引数が既存の音声ファイル**なら `--take` と同じに扱う
// （DAW の外部エディタ・エクスプローラの「プログラムから開く」は `Gliss.exe <WAV>` の形で渡す）。開発版の
// `electron.exe <app のフォルダ>` のフォルダや、存在しないパスは無視する。`--take` があればそちらが優先。
import fs from 'node:fs';
import path from 'node:path';

export const ARG_KEYS = {
  '--take': 'take', '--guide': 'guide', '--project': 'project',
  '--lyrics': 'lyrics', '--guide-lyrics': 'guideLyrics',
  '--project-dir': 'projectDir', '--screenshot': 'screenshot',
  '--user-data-dir': 'userDataDir', '--legacy-user-data-dir': 'legacyUserDataDir',
};

/** `--take` で開ける音声の拡張子（ファイル > 開く… の絞り込みと同じ）。 */
export const AUDIO_EXTS = ['.wav', '.flac', '.aiff', '.aif'];

function isAudioFile(p) {
  if (!AUDIO_EXTS.includes(path.extname(p).toLowerCase())) return false;
  try { return fs.statSync(p).isFile(); } catch { return false; }
}

/**
 * @param {string[]} argv  process.argv.slice(1)
 * @returns {{take?: string, guide?: string, project?: string, lyrics?: string, guideLyrics?: string,
 *            projectDir?: string, screenshot?: string, userDataDir?: string, legacyUserDataDir?: string}}
 */
export function parseArgs(argv) {
  const out = {};
  let positional = null;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (ARG_KEYS[a]) { out[ARG_KEYS[a]] = argv[++i]; continue; }
    const eq = a.indexOf('=');
    if (eq > 0 && ARG_KEYS[a.slice(0, eq)]) { out[ARG_KEYS[a.slice(0, eq)]] = a.slice(eq + 1); continue; }
    if (a.startsWith('-')) continue;                      // Chromium・Electron のスイッチ（--mute など）
    if (positional === null && isAudioFile(a)) positional = path.resolve(a);
  }
  if (!out.take && positional) out.take = positional;
  return out;
}
