// 聞き取り（区間の音声認識で歌詞を確かめる。issue #54）。
// 実モデルは使わない: エンジンの偽の認識器（GLISS_ASR_FAKE の JSON。書き換えるとその場で動きが変わる）で、
// 初回の確認ダイアログ → 取得の進み具合（取り消し）→ 区間の上の進み具合 → 候補を薄く表示 →
// Enter で採用・Ctrl+Z で戻す・Esc で捨てる・ダブルクリックで直して採用、と、使えないときの理由を確かめる。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C');

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const REPO = path.dirname(APP);
const TAKE = M.clip('C');
const PROJECT = path.join(REPO, 'projects', '_test-asr');
const USERDATA = `${PROJECT}-userdata`;
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-asr-spec-'));
const FAKE = path.join(TMP, 'fake-asr.json');
const MODELS = path.join(TMP, 'models');
const TEXT = 'さくらさくら';

let app;
let win;

function fake(cfg) {
  fs.writeFileSync(FAKE, JSON.stringify({ text: TEXT, delay_sec: 2.0, download_sec: 2.5,
    file_size: 3000000, ...cfg }), 'utf8');
}

test.beforeAll(async () => {
  fs.rmSync(PROJECT, { recursive: true, force: true });
  fs.rmSync(USERDATA, { recursive: true, force: true });
  fake({});
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1',
    GLISS_ASR_FAKE: FAKE, GLISS_ASR_MODELS_DIR: MODELS,
    // (7) で AI とつなぐを開く: 本物の claude と %APPDATA%\Claude の設定には触らない
    GLISS_CLAUDE: '', GLISS_CLAUDE_DESKTOP_CONFIG: path.join(TMP, 'claude_desktop_config.json') };
  delete env.ELECTRON_RUN_AS_NODE;
  app = await electron.launch({ args: [APP, '--take', TAKE, '--project-dir', PROJECT,
    '--user-data-dir', USERDATA, '--mute'], env });
  win = await app.firstWindow();
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  // 確認ダイアログ（main の dialog.showMessageBox）を差し替え、出した中身を覚える
  await app.evaluate(({ dialog }) => {
    globalThis.asrAnswer = 0;
    globalThis.asrAsked = [];
    const orig = dialog.showMessageBox;
    dialog.showMessageBox = async (w, opts) => {
      if (opts?.title === '聞き取りのモデルを取得') {
        globalThis.asrAsked.push(opts);
        return { response: globalThis.asrAnswer };
      }
      return orig.call(dialog, w, opts);
    };
  });
});

test.afterAll(async () => {
  await app?.close();
  fs.rmSync(TMP, { recursive: true, force: true });
});

const asked = () => app.evaluate(() => globalThis.asrAsked.map((o) => o.detail));
const answer = (v) => app.evaluate((_e, a) => { globalThis.asrAnswer = a; }, v);
const statusText = () => win.locator('#status').textContent();

/** 最初の発声の真ん中（編集前の秒）と、そこの歌詞レーン上段の画面の位置。 */
async function lanePoint() {
  const t = await win.evaluate(() => {
    const [a, b] = window.__app.utterances()[0];
    window.__app.S.view = { t0: Math.max(0, a - 0.3), span: b - a + 0.6 };
    window.__app.render();
    return (a + b) / 2;
  });
  const box = await win.locator('#roll').boundingBox();
  const v = await win.evaluate(() => window.__app.view());
  const x = box.x + 44 + (t - v.t0) / v.span * (box.width - 44);
  return { t, x, y: box.y + box.height - 34 };
}

async function openLaneMenu() {
  const p = await lanePoint();
  await win.mouse.click(p.x, p.y, { button: 'right' });
  return win.evaluate(() => window.__app.menuItems());
}

async function waitCandidate() {
  await win.waitForFunction(() => !!window.__app.asr.candidate() && !window.__app.asr.running(),
    null, { timeout: 60000 });
}

test('(1) 初回は確認してから取得し、区間の上に進み具合、候補は薄く出るだけで歌詞は変えない', async () => {
  const info = await win.evaluate(() => window.__app.asr.settled());
  expect(info.available).toBe(true);
  expect(info.installed).toBe(false);
  const items = await openLaneMenu();
  const it = items.find((x) => x.cmd === 'transcribe');
  expect(it).toMatchObject({ label: '聞き取る', disabled: false });
  await win.locator('#menu [data-cmd="transcribe"]').click();

  // 取得の進み具合（真ん中。取り消しのボタン付き）
  await expect(win.locator('#busyCenter')).toBeVisible();
  await expect(win.locator('#busyLabel')).toContainText('聞き取りのモデルを取得している');
  await expect(win.locator('#busyCancel')).toBeVisible();
  const detail = (await asked())[0];
  expect(detail).toContain('大きさ: 3 MB');
  expect(detail).toContain(`保存先: ${path.join(MODELS, 'fake-asr')}`);
  expect(detail).toContain('ライセンス: MIT');

  // 聞き取り中: 区間の枠と、その上の細い進み具合
  await expect(win.locator('#roll rect[data-asr="running"]')).toBeVisible({ timeout: 30000 });
  await expect(win.locator('#busyClip')).toBeVisible();
  const bar = await win.locator('#busyClip').boundingBox();
  const frame = await win.locator('#roll rect[data-asr="running"]').boundingBox();
  expect(Math.abs(bar.x - frame.x)).toBeLessThan(2);
  expect(Math.abs(bar.width - frame.width)).toBeLessThan(2);
  expect(bar.y).toBeLessThan(frame.y);
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-asr-running.png') });

  await waitCandidate();
  const cand = await win.evaluate(() => window.__app.asr.candidate());
  expect(cand.text).toBe(TEXT);
  expect(cand.kana).toBe(TEXT);
  expect(cand.words.length).toBe(TEXT.length);
  const words = win.locator('#roll text[data-asr-word]');
  await expect(words).toHaveCount(TEXT.length);
  expect(await words.first().getAttribute('fill')).toBe('#85858a');
  await expect(win.locator('#roll rect[data-asr="candidate"]')).toBeVisible();
  expect(await statusText()).toContain('Enter で採用');
  expect(await win.evaluate(() => window.__app.lyricsEntries())).toEqual([]);
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-asr-candidate.png') });

  // 候補の上の右クリック: 採用・直して採用・閉じる
  const menu = await openLaneMenu();
  expect(menu.map((x) => [x.label, x.key])).toEqual([
    ['聞き取りの候補を歌詞にする', 'Enter'], ['直して歌詞にする…', 'ダブルクリック'], ['候補を閉じる', 'Esc'],
    ['もう一度聞き取る', '']]);
  await win.keyboard.press('Escape');                   // メニューを閉じる（候補は残る）
  expect(await win.evaluate(() => !!window.__app.asr.candidate())).toBe(true);

  // Esc で捨てる。歌詞は変わらない
  await win.locator('#mock').focus();
  await win.keyboard.press('Escape');
  expect(await win.evaluate(() => window.__app.asr.candidate())).toBeNull();
  await expect(win.locator('#roll [data-asr]')).toHaveCount(0);
  expect(await win.evaluate(() => window.__app.lyricsEntries())).toEqual([]);
});

test('(2) Enter で採用して Ctrl+Z で戻す。ダブルクリックで直して採用', async () => {
  const p = await lanePoint();
  await win.evaluate((t) => { window.__app.asr.transcribeAt(t); }, p.t);
  await waitCandidate();
  expect((await asked()).length).toBe(1);             // 2 回目からは確認しない
  await win.locator('#mock').focus();
  await win.keyboard.press('Enter');
  await win.waitForFunction((text) => window.__app.idle()
    && window.__app.lyricsEntries().some((e) => e.text === text), TEXT, { timeout: 120000 });
  const ent = (await win.evaluate(() => window.__app.lyricsEntries()))[0];
  expect(ent.origin).toBe('confirmed');
  expect(ent.start_sec).toBeLessThan(p.t);
  expect(ent.end_sec).toBeGreaterThan(p.t);
  expect(await win.evaluate(() => window.__app.asr.candidate())).toBeNull();
  expect(await win.evaluate(() => window.__app.undoTitle())).toContain('元に戻す: 歌詞');
  await win.keyboard.press('Control+z');
  await win.waitForFunction(() => window.__app.idle() && window.__app.lyricsEntries().length === 0,
    null, { timeout: 120000 });

  await win.evaluate((t) => { window.__app.asr.transcribeAt(t); }, p.t);
  await waitCandidate();
  const w = await win.locator('#roll text[data-asr-word]').nth(2).boundingBox();
  await win.mouse.dblclick(w.x + w.width / 2, w.y + w.height / 2);
  await expect(win.locator('#lyr')).toBeVisible();
  expect(await win.locator('#lyrIn').inputValue()).toBe(TEXT);
  await win.locator('#lyrIn').fill('さくらさくろ');
  await win.keyboard.press('Enter');
  await win.waitForFunction(() => window.__app.idle()
    && window.__app.lyricsEntries().some((e) => e.text === 'さくらさくろ'), null, { timeout: 120000 });
  expect(await win.evaluate(() => window.__app.asr.candidate())).toBeNull();
  // 今の歌詞がある区間を聞き取ると、違いを知らせる
  await win.evaluate((t) => { window.__app.asr.transcribeAt(t); }, p.t);
  await waitCandidate();
  const cur = await win.evaluate(() => window.__app.asr.candidate().current);
  expect(cur).toMatchObject({ kana: 'さくらさくろ', char_errors: 1, same: false });
  expect(await statusText()).toContain('今の歌詞と 1 字違う');
  await win.locator('#mock').focus();
  await win.keyboard.press('Escape');
  await win.keyboard.press('Control+z');
  await win.waitForFunction(() => window.__app.idle() && window.__app.lyricsEntries().length === 0,
    null, { timeout: 120000 });
});

test('(3) 聞き取り中の Esc で取り消す', async () => {
  const p = await lanePoint();
  await win.evaluate((t) => { window.__app.asr.transcribeAt(t); }, p.t);
  await expect(win.locator('#roll rect[data-asr="running"]')).toBeVisible();
  await win.waitForTimeout(400);
  await win.locator('#mock').focus();
  await win.keyboard.press('Escape');
  await win.waitForFunction(() => !window.__app.asr.running(), null, { timeout: 30000 });
  await expect(win.locator('#status')).toContainText('聞き取りを取り消した');
  expect(await win.evaluate(() => window.__app.asr.candidate())).toBeNull();
});

test('(4) 取得の確認でキャンセル・取得中の取り消しは何も残さない', async () => {
  fs.rmSync(MODELS, { recursive: true, force: true });
  const p = await lanePoint();
  await answer(1);
  await win.evaluate((t) => window.__app.asr.transcribeAt(t), p.t);
  await expect(win.locator('#status')).toContainText('モデルは取得しなかった');
  expect(fs.existsSync(path.join(MODELS, 'fake-asr'))).toBe(false);

  await answer(0);
  await win.evaluate((t) => { window.__app.asr.transcribeAt(t); }, p.t);
  await expect(win.locator('#busyCenter')).toBeVisible();
  await win.waitForTimeout(300);
  await win.keyboard.press('Escape');
  await expect(win.locator('#status')).toContainText('モデルの取得を取り消した');
  await expect(win.locator('#busyCenter')).toBeHidden();
  expect(fs.existsSync(path.join(MODELS, 'fake-asr'))).toBe(false);
  expect(fs.existsSync(path.join(MODELS, 'fake-asr.partial'))).toBe(false);
  expect((await win.evaluate(() => window.__app.asr.refresh())).installed).toBe(false);
});

test('(5) 使えない環境では無効にして理由を出す・ショートカットの設定に載る', async () => {
  fake({ unavailable: 'faster-whisper が入っていない（テスト）' });
  await win.evaluate(() => window.__app.asr.refresh());
  const items = await openLaneMenu();
  const it = items.find((x) => x.cmd === 'transcribe');
  expect(it.disabled).toBe(true);
  expect(await win.locator('#menu [data-cmd="transcribe"]').getAttribute('title'))
    .toContain('faster-whisper が入っていない');
  await win.keyboard.press('Escape');
  const cmds = await win.evaluate(() => window.__app.commands());
  expect(cmds.find((c) => c.id === 'transcribe')).toMatchObject({ label: '聞き取る', group: '歌詞' });
  const p = await lanePoint();
  await win.evaluate((t) => window.__app.asr.transcribeAt(t), p.t);
  await expect(win.locator('#status')).toContainText('使えない（faster-whisper が入っていない');
  fake({});
  await win.evaluate(() => window.__app.asr.refresh());
});

test('(6) 素材全体で 1 つの歌詞のときは、区間の候補で置き換えない', async () => {
  await win.evaluate((t) => window.__app.setLyrics(t, null), M.text('C.lyrics'));
  await win.waitForFunction(() => window.__app.idle()
    && window.__app.lyricsEntries().some((e) => e.start_sec == null), null, { timeout: 120000 });
  const before = await win.evaluate(() => window.__app.lyricsEntries());
  const p = await lanePoint();
  await win.evaluate((t) => { window.__app.asr.transcribeAt(t); }, p.t);
  await waitCandidate();
  await win.locator('#mock').focus();
  await win.keyboard.press('Enter');
  await expect(win.locator('#status')).toContainText('素材全体で 1 つの歌詞がある');
  expect(await win.evaluate(() => window.__app.lyricsEntries())).toEqual(before);
  await win.keyboard.press('Escape');
  await win.keyboard.press('Control+z');
  await win.waitForFunction(() => window.__app.idle() && window.__app.lyricsEntries().length === 0,
    null, { timeout: 120000 });
});

test('(7) 候補を出したまま AI とつなぐ・タイトルバーのメニューを開くと、Esc・Enter はそちらが受ける', async () => {
  const before = await win.evaluate(() => window.__app.lyricsEntries());
  const p = await lanePoint();
  await win.evaluate((t) => { window.__app.asr.transcribeAt(t); }, p.t);
  await waitCandidate();
  const cand = () => win.evaluate(() => window.__app.asr.candidate());
  // ヘルプ > AI とつなぐ…: 開いている間の Enter で候補を採用しない。Esc はダイアログを閉じるだけで、候補は残る
  await win.evaluate(() => window.__app.openAi());
  await expect.poll(() => win.evaluate(() => window.__app.aiOpen())).toBe(true);
  await win.keyboard.press('Enter');
  await win.waitForTimeout(300);
  expect(await cand()).not.toBeNull();
  expect(await win.evaluate(() => window.__app.lyricsEntries())).toEqual(before);
  await win.keyboard.press('Escape');
  await expect.poll(() => win.evaluate(() => window.__app.aiOpen())).toBe(false);
  expect(await cand()).not.toBeNull();
  // Alt でメニューバーへ: Enter は最初のメニューを開き、Esc で 1 段ずつ戻る。候補は採用しない・消えない
  await win.locator('#mock').focus();
  await win.keyboard.press('Alt');
  await expect.poll(() => win.evaluate(() => window.__app.menubar().active)).toBe(true);
  await win.keyboard.press('Enter');
  await expect.poll(() => win.evaluate(() => window.__app.menubar().open.length)).toBe(1);
  await win.keyboard.press('Escape');
  await win.keyboard.press('Escape');
  await expect.poll(() => win.evaluate(() => window.__app.menubar().active)).toBe(false);
  expect(await cand()).not.toBeNull();
  expect(await win.evaluate(() => window.__app.lyricsEntries())).toEqual(before);
  // メニューバーから抜けたら、Esc は候補を閉じる
  await win.locator('#mock').focus();
  await win.keyboard.press('Escape');
  expect(await cand()).toBeNull();
});
