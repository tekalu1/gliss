import os from 'node:os';
import path from 'node:path';

// Playwright（Electron）。解析や再合成でエンジンを待つので、タイムアウトは長め。
//
// **テストでは音を一切出さない**（issue #27。打合せ・画面共有の最中に走ることがある）。
// どのテストも env に process.env を渡して Electron を起動するので、ここで入れておけば全部に効く
// （main.mjs が Chromium の --mute-audio・setAudioMuted、renderer が出力の音量 0 にする）。
// 念のため各テストの起動引数にも `--mute` を付けている。
process.env.VOCAL_EDITOR_MUTE = '1';
// **テストのウィンドウを画面に出さず、前面も奪わない**（main.mjs が透明（opacity 0）で showInactive し、
// 人のマウスは素通しにする）。PC を操作している最中に走らせても、操作を中断させない。show しないままだと
// 撮影（win.screenshot）が返らないので、隠すのではなく透明にする。`--headed`（pnpm test:headed）か VOCAL_EDITOR_HIDDEN=0 で
// ウィンドウを見られる。worker は親の環境変数を継ぐので、判定は最初に config を読む親でだけ効けばよい
if (process.argv.includes('--headed')) process.env.VOCAL_EDITOR_HIDDEN = '0';
process.env.VOCAL_EDITOR_HIDDEN ??= '1';
// 既存の編集操作テストは空の歌詞を前提にする。自動推定は lyrics-auto.spec.js で実モデルを使って検証する。
process.env.VOCAL_ENGINE_AUTO_LYRICS = '0';
// **人の実マウスをテストのウィンドウに入れない**（main.mjs が setIgnoreMouseEvents）。テストの操作は CDP で
// 届くので影響しない。テストのドラッグの最中に人がマウスをウィンドウの上で動かすと、そのドラッグが
// そこで終わり（離したことが届かなかった扱い）、connection.spec (1) などがときどき落ちていた
process.env.VOCAL_EDITOR_IGNORE_MOUSE = '1';
// 聞き取り（音声認識。issue #54）の重みの置き場を一時フォルダにする（利用者の %LOCALAPPDATA%\Gliss\models に触れない。
// テストは実モデルを使わず、asr.spec.js が偽の認識器 GLISS_ASR_FAKE で動かす）
process.env.GLISS_ASR_MODELS_DIR = path.join(os.tmpdir(), 'gliss-test-asr-models');

export default {
  testDir: './tests',
  timeout: 300000,
  expect: { timeout: 60000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: { trace: 'off' },
};
