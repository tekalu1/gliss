// DAW との往復（DAW 連携 段階 1。issue #4）。
//
//   (1) WAV をウィンドウに落とすとテイクとして開く（落とす前はステータス行に案内が出る）
//   (2) Shift を押しながら落とすとガイドとして重ねる（テイクと編集はそのまま）
//   (3) 音声でないファイルを落としても何も変わらない
//   (4) ファイル > 書き出し（Ctrl+E）はダイアログを出さず、元ファイルの隣に `_ve`。2 回目は `_ve(2)`
//       （上書きしない）。元の bext（DAW 上の位置）が入っていて、非編集区間は元と同じ
//
// 落とすファイルは本物のパスを持つ File にする: ページに一時の <input type=file> を作って Playwright の
// setInputFiles で実在のファイルを入れ、その File を DataTransfer に入れて drop を送る
// （`webUtils.getPathForFile` は、ページの中で new File() したものにはパスを返さない）。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2', 'E');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const TAKE = M.clip('C');
const GUIDE = M.clip('C2');
const START = M.clip('E');          // 起動時に開いておく別のテイク
const ROOT = path.join(REPO, 'projects', '_test-daw');
const MEDIA = path.join(ROOT, 'Media');                // DAW のソングの Media フォルダの代わり
const VO = path.join(MEDIA, 'vo.wav');
const GD = path.join(MEDIA, 'guide.wav');
const NOTE = path.join(MEDIA, 'memo.txt');
const USERDATA = path.join(ROOT, 'userdata');
const TREF = 4771683;

let app;
let win;
const errors = [];

// ---------------------------------------------------------------- RIFF を手で読む・組む
function chunks(buf) {
  const out = [];
  let i = 12;
  while (i + 8 <= buf.length) {
    const id = buf.toString('latin1', i, i + 4);
    const n = buf.readUInt32LE(i + 4);
    out.push({ id, body: buf.subarray(i + 8, i + 8 + n) });
    i += 8 + n + (n & 1);
  }
  return out;
}

/** 元の WAV の fmt の前に bext（TimeReference = tref）を足したもの（Studio One の録音の代わり）。 */
function withBext(src, tref) {
  const bext = Buffer.alloc(602);
  bext.write('vocal take', 0, 'latin1');
  bext.writeBigUInt64LE(BigInt(tref), 338);
  bext.writeUInt16LE(1, 346);
  const parts = [Buffer.from('bext', 'latin1'), Buffer.alloc(4), bext];
  parts[1].writeUInt32LE(bext.length);
  const b = fs.readFileSync(src);
  const body = Buffer.concat([Buffer.from('WAVE', 'latin1'), ...parts, b.subarray(12)]);
  const head = Buffer.from('RIFF\0\0\0\0', 'latin1');
  head.writeUInt32LE(body.length, 4);
  return Buffer.concat([head, body]);
}

function tref(file) {
  const b = chunks(fs.readFileSync(file)).find((c) => c.id === 'bext');
  return b ? Number(b.body.readBigUInt64LE(338)) : null;
}

/** data の中身（int24 → 数値の配列ではなく、バイト列のまま比べる）。 */
function dataOf(file) {
  return chunks(fs.readFileSync(file)).find((c) => c.id === 'data').body;
}

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  // 落として開いたプロジェクトの置き場（既定の置き場）もテスト用の場所にする
  env.VOCAL_ENGINE_PROJECTS = path.join(ROOT, 'projects');
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(MEDIA, { recursive: true });
  fs.writeFileSync(VO, withBext(TAKE, TREF));
  fs.copyFileSync(GUIDE, GD);
  fs.writeFileSync(NOTE, 'not audio');
  app = await electron.launch({
    args: [APP, '--take', START, '--project-dir', path.join(ROOT, 'start'),
      '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console: ${m.text()}`);
  });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 240000 });
}

/** 実在のファイルをウィンドウに落とす（dragover → drop）。dragover 後のステータスを返す。 */
async function drop(file, { shift = false } = {}) {
  await win.evaluate(() => {
    let el = document.getElementById('__dropInput');
    if (!el) {
      el = document.createElement('input');
      el.type = 'file';
      el.id = '__dropInput';
      el.style.display = 'none';
      document.body.appendChild(el);
    }
  });
  await win.setInputFiles('#__dropInput', file);
  return win.evaluate(async (sh) => {
    const f = document.getElementById('__dropInput').files[0];
    const dt = new DataTransfer();
    dt.items.add(f);
    const target = document.querySelector('#roll');
    const init = { bubbles: true, cancelable: true, dataTransfer: dt, shiftKey: sh };
    target.dispatchEvent(new DragEvent('dragenter', init));
    target.dispatchEvent(new DragEvent('dragover', init));
    const hint = document.querySelector('#status').textContent;
    target.dispatchEvent(new DragEvent('drop', init));
    return hint;
  }, shift);
}

const takePath = () => win.evaluate(() => window.__app.S.take?.path || null);
const guidePath = () => win.evaluate(() => window.__app.S.guide?.path || null);

test('(1) WAV を落とすとテイクとして開く', async () => {
  expect(path.basename(await takePath())).toBe(path.basename(START));
  const hint = await drop(VO);
  expect(hint).toContain('テイクとして開く');
  await win.waitForFunction((p) => window.__app.S.take?.path === p, VO, { timeout: 240000 });
  await win.waitForFunction(() => window.__app.ready() && window.__app.S.pitched.length > 0);
  await settle();
  expect(await guidePath()).toBeNull();
  // 既定の置き場（素材のハッシュで決まる場所）はテスト用の場所の下
  const dir = await win.evaluate(() => window.__app.S.projectDir);
  expect(path.resolve(dir).startsWith(path.resolve(ROOT))).toBe(true);
});

test('(2) Shift を押しながら落とすとガイドとして重ねる（テイクと編集はそのまま）', async () => {
  await win.evaluate(async () => {
    const n = window.__app.S.pitched[1];
    await window.api.call('shift_pitch', { note_id: n.id, cents: 100, author: 'human' });
    await window.__app.refresh();
  });
  await settle();
  const hint = await drop(GD, { shift: true });
  expect(hint).toContain('ガイドとして重ねる');
  await win.waitForFunction((p) => window.__app.S.guide?.path === p, GD, { timeout: 240000 });
  await settle();
  expect(await takePath()).toBe(VO);
  const edits = await win.evaluate(() => window.__app.S.vd.edits.length);
  expect(edits).toBe(1);                                   // ガイドを重ねても編集は消えない
});

test('(3) 音声でないファイルを落としても何も変わらない', async () => {
  await drop(NOTE);
  await expect.poll(() => win.evaluate(() => window.__app.status())).toContain('音声ファイルではない');
  expect(await takePath()).toBe(VO);
  expect(await guidePath()).toBe(GD);
});

test('(3b) 開いている途中の 2 つ目のドロップは断る・同じファイルは開き直さない・別テイクに替えて戻すと編集が残る', async () => {
  // 同じテイクをもう一度落としても開き直さない（表示範囲も変わらない）
  expect(await win.evaluate((p) => window.__app.openDropped([p]), VO)).toBe(false);
  expect(await win.evaluate(() => window.__app.status())).toContain('もう開いている');
  expect(await win.evaluate((p) => window.__app.openDropped([p], { guide: true }), VO)).toBe(false);
  // 開いている途中（解析中）にもう 1 つ落とすと断る（open_project が 2 本並ばない）
  const [a, b] = await win.evaluate(async ([x, y]) => {
    const first = window.__app.openDropped([x]);
    const second = await window.__app.openDropped([y]);
    return [await first, second];
  }, [START, GD]);
  expect(a).toBe(true);
  expect(b).toBe(false);
  await settle();
  expect(path.basename(await takePath())).toBe(path.basename(START));
  expect(await guidePath()).toBe(GD);                      // ガイドはそのまま
  // 元のテイクに戻すと、前に入れた編集がそのまま残っている（プロジェクトの project.json から）
  await drop(VO);
  await win.waitForFunction((p) => window.__app.S.take?.path === p, VO, { timeout: 240000 });
  await settle();
  expect(await win.evaluate(() => window.__app.S.vd.edits.length)).toBe(1);
});

test('(4) 書き出しは元ファイルの隣に _ve（上書きしない）・bext の位置と非編集区間を保つ', async () => {
  const first = path.join(MEDIA, 'vo_ve.wav');
  const second = path.join(MEDIA, 'vo_ve(2).wav');
  // ダイアログは出さない（出たら失敗にする）
  await app.evaluate(async ({ dialog }) => {
    dialog.showSaveDialog = async () => { throw new Error('ダイアログを出した'); };
  });
  const r = await win.evaluate(() => window.__app.onMenu({ cmd: 'export' }));
  await settle();
  expect(r.path).toBe(first);
  expect(fs.existsSync(first)).toBe(true);
  expect(r.bwf.time_reference).toBe(TREF);
  expect(tref(first)).toBe(TREF);                          // DAW 上の位置が元と同じ
  const st = await win.evaluate(() => window.__app.status());
  expect(st).toContain('vo_ve.wav');
  expect(st).toContain('DAW 上の位置');

  // 長さ・形式が同じ。差し替えた区間の外はバイト単位で元と同じ（24 bit モノラル = 3 バイト/サンプル）
  const a = dataOf(VO);
  const b = dataOf(first);
  expect(b.length).toBe(a.length);
  const fmtA = chunks(fs.readFileSync(VO)).find((c) => c.id === 'fmt ').body;
  const fmtB = chunks(fs.readFileSync(first)).find((c) => c.id === 'fmt ').body;
  expect(fmtB.subarray(0, 16).subarray(2)).toEqual(fmtA.subarray(0, 16).subarray(2));
  const bytesPerFrame = fmtA.readUInt16LE(12);
  const sr = fmtA.readUInt32LE(4);
  expect(r.replaced_spans_sec.length).toBeGreaterThan(0);
  let prev = 0;
  for (const [s, e] of r.replaced_spans_sec) {
    const i = Math.round(s * sr) * bytesPerFrame;
    expect(b.subarray(prev, i).equals(a.subarray(prev, i))).toBe(true);
    prev = Math.round(e * sr) * bytesPerFrame;
  }
  expect(b.subarray(prev).equals(a.subarray(prev))).toBe(true);
  expect(b.equals(a)).toBe(false);                         // 編集は入っている

  // 2 回目は上書きしない
  const before = fs.readFileSync(first);
  const r2 = await win.evaluate(() => window.__app.onMenu({ cmd: 'export' }));
  await settle();
  expect(r2.path).toBe(second);
  expect(fs.readFileSync(first).equals(before)).toBe(true);
  expect(tref(second)).toBe(TREF);
  expect(fs.readdirSync(MEDIA).filter((f) => f.includes('tmp'))).toEqual([]);
});

test('実行時のコンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});
