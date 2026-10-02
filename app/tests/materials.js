// テスト素材（リポジトリに入れない実際の歌声）の場所と、素材に結び付いたデータ。
//
// engine/tests/materials.py と同じく、環境変数 GLISS_TEST_MATERIALS で手元の素材のフォルダを指す。フォルダの
// materials.json に素材の一覧（記号 → ファイル名）と、素材に結び付いたデータ（歌詞など）を書く（docs/testing.md）。
// 素材の歌詞やファイル名はテストのコードに書かない。コードは記号（'C' など）とデータのキーで引く。
//
// 素材を開くテストは、素材と解析モデルの重みがそろっていなければ skip する（skipUnlessReady）。
// 重みが無いまま素材を開くと、画面が初回の「モデルの準備」に止まり、解析を待つテストが時間切れになる。
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { modelDirectory } from '../model-download.mjs';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const DIR = process.env.GLISS_TEST_MATERIALS ? path.resolve(process.env.GLISS_TEST_MATERIALS) : '';
const MISSING = path.join(os.tmpdir(), 'gliss-no-materials');

function load() {
  const p = DIR && path.join(DIR, 'materials.json');
  if (!p || !fs.existsSync(p)) return {};
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

const DATA = load();

/** 記号の素材の WAV のパス。素材が無いときは存在しないパス。 */
export function clip(symbol) {
  const name = DATA.clips?.[symbol];
  return name ? path.resolve(DIR, name) : path.join(MISSING, `${symbol}.wav`);
}

/** 素材に結び付いたデータ（歌詞など）。素材が無いときは fallback。 */
export function data(key, fallback = undefined) {
  return DATA.data?.[key] ?? fallback;
}

/** 文字列のデータ。素材が無いときは空文字。 */
export function text(key) {
  const v = data(key);
  return typeof v === 'string' ? v : '';
}

/** トラックの既定の名前（ファイル名から拡張子を除いたもの）。 */
export function stem(file) {
  return path.basename(file, path.extname(file));
}

export function available(...symbols) {
  return Object.keys(DATA).length > 0 && symbols.every((s) => fs.existsSync(clip(s)));
}

/** 開発版の画面が使う重みの置き場に RMVPE の重みがあるか（main.mjs と同じ決め方）。 */
export function modelsReady(env = process.env) {
  const dir = modelDirectory({ packaged: false, env, localAppData: env.LOCALAPPDATA, repo: REPO });
  return fs.existsSync(path.join(dir, 'rmvpe.onnx'));
}

/** ファイルの頭で呼ぶ: 素材か重みが無ければ、このファイルのテストを全部 skip する。 */
export function skipUnlessReady(test, ...symbols) {
  test.skip(!available(...symbols), `テスト素材 ${symbols.join(', ')} が無い（GLISS_TEST_MATERIALS）`);
  test.skip(!modelsReady(), '解析モデルの重みが無い（VOCAL_ENGINE_MODELS_DIR か初回の画面で取得）');
}
