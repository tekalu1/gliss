// 段階2: 歌詞・音素レーンと音素境界のタイミング編集。
//
//   (1) --lyrics で起動すると、レーンにかな 1 段＋音素 1 段の文字が出る
//   (2) ピアノロールの音素境界をドラッグすると move_boundary が呼ばれる
//   (3) 「ガイドに合わせる」のタイミングスライダーで**母音だけ**が動き、離した後も同じ形
//   (4) 歌詞レーンのダブルクリックで 1 行の入力欄が出る
//   (5) 右クリック（issue #17）: 歌詞レーン（編集…／この区間の歌詞を消す／読み込む…）と音素（子音｜母音の境目を元に戻す）
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const TAKE = M.clip('C');
const GUIDE = M.clip('C2');
const LYRICS = M.text('C.lyrics');            // 素材 C の歌詞（materials.json）
// 段階1 のテスト（app.spec.js）と**プロジェクトを分ける**。
// 同じ置き場を使うと歌詞と編集リストが混ざる（音素があると音符端のつまみが消える）。
const PROJECT = path.join(REPO, 'projects', '_test-phonemes');
// 設定（最近使ったファイル・前回の表示範囲・ウィンドウの位置）も毎回まっさらにする。人が使った
// ときの表示範囲を引き継ぐと、狙うノートが画面の外に出てドラッグが外れる（人の設定も汚さない）。
const USERDATA = `${PROJECT}-userdata`;

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  fs.rmSync(PROJECT, { recursive: true, force: true });
  fs.rmSync(USERDATA, { recursive: true, force: true });
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--guide', GUIDE, '--lyrics', LYRICS,
      '--guide-lyrics', LYRICS, '--project-dir', PROJECT,
      '--user-data-dir', USERDATA, '--mute'],
    env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console: ${m.text()}`);
  });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  // 歌詞つきで開いたので音素が出ているはず
  await win.waitForFunction(() => window.__app.phonemes().length > 0, null,
    { timeout: 240000 });
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  // S.busy だけだと、離した後に計画の到着や前の編集を待っている間（まだ busy でない）を見落とす
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
}

const phonemes = () => win.evaluate(() => window.__app.phonemes());
const boundaries = () => win.evaluate(() => window.__app.boundaries());
const lengths = () => win.evaluate(() => window.__app.localLengths());
const edits = () => win.evaluate(() => window.__app.S.vd.edits);

test('(1) 歌詞・音素レーンに文字が出る', async () => {
  expect(await win.evaluate(() => window.__app.lyrics())).toBe(LYRICS);
  const ph = await phonemes();
  const voiced = ph.filter((p) => p.label !== 'silence');
  expect(voiced.length).toBe(31);                       // C は 17 音節 31 音素
  expect(voiced.map((p) => p.text).join(' ')).toBe(
    'p o m a i r a p o m a i r a h o n a t o N z u r a s u r u d e');

  // かな 1 段（上）＋音素 1 段（下）。前回の表示範囲（userData の state.json。別の実行と
  // 共有される）に左右されないよう、素材全体を表示してから数える
  await win.evaluate(() => {
    window.__app.S.view = { t0: 0, span: window.__app.totalSec() };
    window.__app.render();
  });
  const kana = await win.locator('#roll text[data-kana]').allTextContents();
  expect(kana.length).toBe(17);
  expect(kana.slice(0, 4)).toEqual(['ぽ', 'ま', 'い', 'ら']);
  const phText = await win.locator('#roll text[data-ph]').allTextContents();
  expect(phText.length).toBeGreaterThan(20);
  expect(phText).toContain('p');
  expect(phText).toContain('a');

  // 子音と母音は明度差で描き分ける（モックと同じ）
  const cons = win.locator('#roll text[data-ph]').filter({ hasText: /^p$/ }).first();
  const vow = win.locator('#roll text[data-ph]').filter({ hasText: /^a$/ }).first();
  expect(await cons.getAttribute('fill')).toBe('#5c5c62');
  expect(await vow.getAttribute('fill')).toBe('#9a9a9e');

  // ピアノロールの境界線は**音素境界**（音符境界ではない）
  const bd = await boundaries();
  expect(bd.length).toBe((await phonemes()).length + 1);
  const rollBd = await win.evaluate(() => window.__app.S.vd.boundaries);
  expect(rollBd.length).toBe(bd.length);
  expect(rollBd.every((b) => b.kind !== 'note')).toBe(true);

  fs.mkdirSync(path.join(APP, 'screenshots'), { recursive: true });
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-phonemes.png') });
});

test('(2) 音素境界のドラッグで move_boundary が呼ばれる', async () => {
  expect((await edits()).length).toBe(0);
  // 両隣に余裕のある境界を選ぶ（20 ms の下限に当たらないように）
  const pick = await win.evaluate(() => {
    const ph = window.__app.phonemes();
    const bs = window.__app.boundaries();
    for (let i = 1; i < bs.length - 1; i++) {
      if (ph[i - 1].len > 0.09 && ph[i].len > 0.09) return bs[i].id;
    }
    return null;
  });
  expect(pick).toBeTruthy();

  const handle = win.locator(`#roll rect[data-bound="${pick}"]`).first();
  const box = await handle.boundingBox();
  await win.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await win.mouse.down();
  await win.mouse.move(box.x + box.width / 2 + 14, box.y + box.height / 2, { steps: 8 });
  // ドラッグ中はツールチップに ms が出る（エンジンはまだ呼ばない）
  await expect(win.locator('#roll text').filter({ hasText: /ms$/ }).first()).toBeVisible();
  expect((await edits()).length).toBe(0);
  await win.mouse.up();
  await settle();

  const es = await edits();
  expect(es.length).toBe(1);
  expect(es[0].kind).toBe('move_boundary');
  expect(es[0].target.boundary_id).toBe(pick);
  expect(es[0].params.ms).toBeGreaterThan(0);
  // 左の音素が伸び、右が縮む（合計は変わらない）
  expect(es[0].params.left_ratio).toBeGreaterThan(1);
  expect(es[0].params.right_ratio).toBeLessThan(1);

  await win.evaluate(async () => {
    await window.api.call('undo', {});
    await window.__app.refresh();
  });
  expect((await edits()).length).toBe(0);
});

test('(3) タイミングのスライダーで母音だけが動き、離した後も同じ形', async () => {
  const before = await lengths();
  await win.locator('#bMacro').click();
  await expect(win.locator('#pop')).toBeVisible();
  // 開いた時点でエンジンが 100% の計画を返す（対象ノートが決まった時点で 1 回だけ）
  await win.waitForFunction(() => window.__app.plan()?.kind === 'guide', null, { timeout: 60000 });
  await win.evaluate(() => {
    const el = document.querySelector('#popTime');
    el.value = '70';
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
  const after = await lengths();
  expect((await edits()).length).toBe(0);              // まだエンジンは呼んでいない

  const moved = { consonant: 0, vowel: 0 };
  for (let i = 0; i < before.length; i++) {
    if (Math.abs(after[i].len - before[i].len) > 1e-4) moved[before[i].label] ??= 0;
    if (Math.abs(after[i].len - before[i].len) > 1e-4) moved[before[i].label] += 1;
  }
  expect(moved.vowel).toBeGreaterThan(0);
  expect(moved.consonant ?? 0).toBe(0);                // **子音の長さは変わらない**

  // 離すとエンジンが同じ計画を同じ強度で確定する。確定後の長さ = プレビューの長さ
  await win.evaluate(() => {
    document.querySelector('#popTime').dispatchEvent(new Event('change', { bubbles: true }));
  });
  await settle();
  const es = await edits();
  expect(es.length).toBeGreaterThan(0);
  expect(es.every((e) => ['stretch', 'crop', 'silence'].includes(e.kind))).toBe(true);
  const done = await lengths();
  for (let i = 0; i < done.length; i++) {
    expect(Math.abs(done[i].len - after[i].len)).toBeLessThan(0.0005);   // 0.5 ms
    if (done[i].label === 'consonant') {
      expect(Math.abs(done[i].len - before[i].len)).toBeLessThan(0.0002);
    }
  }
  await win.keyboard.press('Escape');
  await win.evaluate(async () => {
    for (let i = 0; i < 30; i += 1) {
      const r = await window.api.call('undo', {});
      if (!r || r.ok === false) break;
    }
    await window.__app.refresh();
  });
  expect((await edits()).length).toBe(0);
});

test('(4) 歌詞レーンのダブルクリックで入力欄が出る', async () => {
  const svg = await win.locator('#roll').boundingBox();
  await win.mouse.dblclick(svg.x + svg.width / 2, svg.y + svg.height - 12);
  await expect(win.locator('#lyr')).toBeVisible();
  expect(await win.locator('#lyrIn').inputValue()).toBe(LYRICS);
  await win.keyboard.press('Escape');
  await expect(win.locator('#lyr')).toBeHidden();
});

test('(5) 右クリック: 歌詞レーンと音素（子音｜母音の境目を元に戻す）', async () => {
  const items = () => win.evaluate(() => window.__app.menuItems());
  const svg = await win.locator('#roll').boundingBox();
  // 歌詞のある区間の上段: 編集… / 消す / 聞き取る / 読み込む…
  // （聞き取るが使えるかは faster-whisper が入っているかによる。asr.spec.js で両方を見る）
  await win.mouse.click(svg.x + svg.width / 2, svg.y + svg.height - 34, { button: 'right' });
  expect((await items()).map((x) => [x.label, x.cmd === 'transcribe' ? null : x.disabled])).toEqual([
    ['歌詞を編集…', false], ['この区間の歌詞を消す', false], ['聞き取る', null], ['歌詞を読み込む…', false]]);
  await win.keyboard.press('Escape');
  // 境界を動かす → その左の音素（下段）を右クリック →「子音｜母音の境目を元に戻す」
  const pick = await win.evaluate(() => {
    const ph = window.__app.phonemes();
    const bs = window.__app.boundaries();
    for (let i = 1; i < bs.length - 1; i++) {
      if (ph[i - 1].len > 0.09 && ph[i].len > 0.09) return bs[i].id;
    }
    return null;
  });
  const box = await win.locator(`#roll rect[data-bound="${pick}"]`).first().boundingBox();
  await win.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await win.mouse.down();
  await win.mouse.move(box.x + box.width / 2 + 14, box.y + box.height / 2, { steps: 8 });
  await win.mouse.up();
  await settle();
  expect((await edits()).map((e) => e.kind)).toEqual(['move_boundary']);
  const bx = await win.locator(`#roll rect[data-bound="${pick}"]`).first().boundingBox();
  await win.mouse.click(bx.x - 6, svg.y + svg.height - 8, { button: 'right' });
  const it = await items();
  expect(it.map((x) => [x.label, x.key, x.disabled])).toEqual([
    ['子音｜母音の境目を元に戻す', '', false], ['歌詞を編集…', 'ダブルクリック', false]]);
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-menu-phoneme.png') });
  await win.locator('#menu [data-item="reset-boundary"]').click();
  await settle();
  expect((await edits()).length).toBe(0);
  expect(await win.evaluate(() => window.__app.undoTitle())).toBe('元に戻す: 音素の境目を元に戻す（Ctrl+Z）');
  await win.keyboard.press('Control+z');
  await settle();
  expect((await edits()).length).toBe(1);
  await win.evaluate(async () => {
    await window.api.call('undo', {});
    await window.__app.refresh();
  });
  expect((await edits()).length).toBe(0);
});

test('実行時のコンソールエラーが無い', async () => {
  const consonants = await win.locator('#roll path[data-nopitch][data-cons]').evaluateAll((paths) => paths.map((p) => ({
    d: p.getAttribute('d'), fill: p.getAttribute('fill'),
  })));
  expect(consonants.length).toBeGreaterThan(0);
  for (const { d, fill } of consonants) {
    expect(d).toMatch(/^M/);
    expect(fill).toMatch(/^#[0-9a-f]{6}$/i);
  }
  expect(errors).toEqual([]);
});
