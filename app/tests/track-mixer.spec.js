// トラックビューの見出し: 音量のスライダー・パンのノブ・見出しの幅（docs/track-view.md「見出しの音量・パンと幅」）。
// 合成の音だけで動く（素材は使わない。解析モデルの重みがあれば開ける）。
//
//   (X1) 見出しは 2 段（名前・M・S の下に音量・パン）。role="slider"・「<トラック名> の音量」・M／S の名前にもトラック名
//   (X2) 音量: ドラッグで変わる・吹き出し・ダブルクリックで 0 dB・Shift で細かく・ホイールでは変えない
//   (X3) 音量: キー（←→ 0.5 dB・Shift 0.1・Home・End）。再生位置のコマンドには渡らない
//   (X4) パン: 上下のドラッグ・ダブルクリックで中央・キー・読み上げの値
//   (X5) 聞こえないトラックは 2 つの部品を薄くする（値は残す）
//   (X6) 取り消しの対象外（Ctrl+Z は別の操作に届く）・session.json に保存される
//   (X7) 再生中に効く（GainNode の音量・StereoPanner のパン）
//   (X8) 見出しの幅: ドラッグ・ダブルクリックで 160・キー・上限と下限・ルーラーとレーンがそろう・150 px 未満は dB を隠す
//   (X9) 高さ 40 px 未満では 2 段目を畳む（値は名前のツールチップ）
//   (X10) 開き直しても残る（音量・パンはセッション、幅は表示の設定）
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { dbToPos, dbToGain } from '../renderer/mixer.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'output', 'track-mixer');
const MEDIA = path.join(ROOT, 'Media');
const PROJECT = path.join(ROOT, 'proj');
const USERDATA = path.join(ROOT, 'userdata');

function writeWav(file, channels, phase) {
  const sr = 48000; const frames = sr * 4; const bps = channels * 2;
  const buf = Buffer.alloc(44 + frames * bps);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(channels, 22);
  buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * bps, 28);
  buf.writeUInt16LE(bps, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(frames * bps, 40);
  for (let i = 0; i < frames; i++) {
    const t = i / sr;
    const env = t < 0.25 || (t > 1.4 && t < 1.65) ? 0 : 0.35 + 0.4 * Math.abs(Math.sin(t * 2.2));
    for (let c = 0; c < channels; c++) {
      const a = env * 0.7 * Math.sin(2 * Math.PI * (180 + t * 20 + c * 89) * t + phase);
      buf.writeInt16LE(Math.max(-32767, Math.min(32767, Math.round(a * 32767))), 44 + i * bps + c * 2);
    }
  }
  fs.writeFileSync(file, buf);
}

const TAKE = path.join(MEDIA, 'take.wav');
const VOX2 = path.join(MEDIA, 'vox2.wav');
const INST = path.join(MEDIA, 'Inst_mix.wav');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

async function launch() {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_ENGINE_PROJECTS: path.join(ROOT, 'projects') };
  delete env.ELECTRON_RUN_AS_NODE;
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--project-dir', PROJECT, '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
}

test.beforeAll(async () => {
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(MEDIA, { recursive: true });
  writeWav(TAKE, 1, 0); writeWav(VOX2, 1, 1); writeWav(INST, 2, 0.5);
  await launch();
  await settle();
  await win.evaluate(async ([a, b]) => {
    await window.api.call('add_track', { path: a });
    await window.api.call('add_track', { path: b });
    await window.__app.loadSession();
  }, [VOX2, INST]);
  await win.waitForFunction(() => window.__app.S.tracks.length === 3, null, { timeout: 60000 });
});

test.afterAll(async () => {
  await app?.close();
  if (!path.resolve(ROOT).startsWith(path.resolve(REPO) + path.sep)) throw new Error('fixture path outside worktree');
  fs.rmSync(ROOT, { recursive: true, force: true });
  expect(errors).toEqual([]);
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 240000 });
}
const tracks = () => win.evaluate(() => window.__app.tracks());
const byName = async (name) => (await tracks()).find((t) => t.name === name);
const row = (t) => win.locator(`#heads .th[data-id="${t.id}"]`);
/** エンジンが持っている値（画面の値とは別に、保存まで届いたか）。 */
const engineTrack = async (id) => (await win.evaluate(() => window.api.call('list_tracks', {}))).tracks.find((t) => t.id === id);
const undoLabel = async () => (await win.evaluate(() => window.__app.hist()))?.undo?.label ?? null;
const state = () => win.evaluate(() => window.__app.tracksState());
const num = (v) => Math.round(v * 1000) / 1000;

/** つまみ・ノブを (dx, dy) だけドラッグする。途中で mid（押している間の確認）を呼ぶ。 */
async function drag(loc, from, dx, dy, { shift = false, mid = null } = {}) {
  await win.waitForTimeout(450);                       // 前の押下から 400 ms あける（近くの 2 回押しは既定値に戻す操作）
  const b = await loc.boundingBox();
  const x0 = b.x + from.x; const y0 = b.y + from.y;
  await win.mouse.move(x0, y0);
  await win.mouse.down();
  if (shift) await win.keyboard.down('Shift');
  for (let i = 1; i <= 6; i++) await win.mouse.move(x0 + dx * i / 6, y0 + dy * i / 6);
  if (mid) await mid();
  if (shift) await win.keyboard.up('Shift');
  await win.mouse.up();
}

/** (x, y) を 2 回押す（既定値に戻す）。前の押下とは 400 ms あける。 */
async function twice(x, y) {
  await win.waitForTimeout(450);
  await win.mouse.move(x, y);
  await win.mouse.down(); await win.mouse.up();
  await win.mouse.down(); await win.mouse.up();
}

/** スライダーのつまみの今の位置（スライダーの左端からの px）。 */
async function knobX(t) {
  const b = await row(t).locator('.vol').boundingBox();
  const g = (await byName(t.name)).gain_db;
  return { b, x: dbToPos(g) * b.width };
}

test('(X1) 見出しは 2 段で、読み上げの名前にトラック名が入る', async () => {
  const ts = await tracks();
  expect(ts.map((t) => [t.gain_db, t.pan])).toEqual([[0, 0], [0, 0], [0, 0]]);
  for (const t of ts) {
    const r = row(t);
    await expect(r.locator('.r1 .nm')).toHaveText(t.name);
    await expect(r.locator('.r1 button[data-act="m"]')).toHaveAttribute('aria-label', `${t.name} のミュート`);
    await expect(r.locator('.r1 button[data-act="s"]')).toHaveAttribute('aria-label', `${t.name} のソロ`);
    const vol = r.locator('.r2 .vol');
    await expect(vol).toHaveAttribute('role', 'slider');
    await expect(vol).toHaveAttribute('aria-label', `${t.name} の音量`);
    await expect(vol).toHaveAttribute('aria-valuetext', '0.0 dB');
    const pan = r.locator('.r2 .pan');
    await expect(pan).toHaveAttribute('role', 'slider');
    await expect(pan).toHaveAttribute('aria-label', `${t.name} のパン`);
    await expect(pan).toHaveAttribute('aria-valuetext', '中央');
    // 2 段は行の中に収まる
    const rb = await r.boundingBox(); const b2 = await r.locator('.r2').boundingBox();
    expect(b2.y + b2.height).toBeLessThanOrEqual(rb.y + rb.height + 0.5);
  }
  // 行の高さは今までどおり（縦ズームの高さ）
  expect((await state()).trackH).toBe(44);
  expect((await row(ts[0]).boundingBox()).height).toBeCloseTo(44, 0);
});

test('(X2) 音量: ドラッグ・吹き出し・ダブルクリックで 0 dB・Shift で細かく・ホイールでは変えない', async () => {
  const t = await byName('vox2');
  const vol = row(t).locator('.vol');
  const { b, x } = await knobX(t);
  const history0 = await undoLabel();
  // つまみを左へ 20%（0 dB の 80% → 60%）
  await drag(vol, { x, y: 8 }, -0.2 * b.width, 0, {
    mid: async () => {
      const bub = win.locator('#tvBub');
      await expect(bub).toBeVisible();
      await expect(bub).toHaveText(/^−\d+\.\d dB$/);
      // 吹き出しはつまみの上
      const bb = await bub.boundingBox(); const vb = await vol.boundingBox();
      expect(bb.y + bb.height).toBeLessThanOrEqual(vb.y + 1);
      // 描き直しても同じ要素ではないので、押している間の見た目はクラスで見る
      await expect(row(t).locator('.vol.drag')).toHaveCount(1);
    },
  });
  await expect(win.locator('#tvBub')).toBeHidden();
  const g1 = (await byName('vox2')).gain_db;
  expect(g1).toBeCloseTo(-7.7, 0);                      // 20 * N * log10(0.6 / 0.8)
  expect((await state()).heads.find((h) => h.id === t.id).gain).toBeCloseTo(g1, 1);
  await expect(row(t).locator('.vv')).toHaveText(/^−7\.\d$/);
  await expect(row(t).locator('.vv')).toHaveClass(/chg/);
  await expect.poll(async () => (await engineTrack(t.id)).gain_db, { timeout: 10000 }).toBeCloseTo(g1, 2);
  // ほかのトラックは変わらない
  expect((await byName('take')).gain_db).toBe(0);
  // ダブルクリック（2 回押す）で 0 dB
  const kn = await knobX(t);
  await twice(kn.b.x + kn.x, kn.b.y + 8);
  expect((await byName('vox2')).gain_db).toBe(0);
  await expect(row(t).locator('.vv')).not.toHaveClass(/chg/);
  await expect.poll(async () => (await engineTrack(t.id)).gain_db, { timeout: 10000 }).toBe(0);
  // Shift で細かく: 同じ移動量で、変わる量が 1/10 に近い
  const k2 = await knobX(t);
  await drag(vol, { x: k2.x, y: 8 }, -0.2 * k2.b.width, 0);
  const coarse = (await byName('vox2')).gain_db;
  await twice(k2.b.x + (await knobX(t)).x, k2.b.y + 8);                   // 0 dB へ
  expect((await byName('vox2')).gain_db).toBe(0);
  await drag(vol, { x: k2.x, y: 8 }, -0.2 * k2.b.width, 0, { shift: true });
  const fine = (await byName('vox2')).gain_db;
  expect(coarse).toBeLessThan(-7);
  expect(fine).toBeLessThan(0);
  expect(Math.abs(fine)).toBeLessThan(Math.abs(coarse) / 4);
  // 0 dB に戻す
  const k3 = await knobX(t);
  await twice(k3.b.x + k3.x, k3.b.y + 8);
  expect((await byName('vox2')).gain_db).toBe(0);
  // ホイールでは変えない
  await win.mouse.move(k3.b.x + k3.x, k3.b.y + 8);
  await win.mouse.wheel(0, -120);
  await win.mouse.wheel(0, 120);
  expect((await byName('vox2')).gain_db).toBe(0);
  // ここまでの操作で履歴は増えない
  expect(await undoLabel()).toBe(history0);
});

test('(X3) 音量: キーで動かす（←→ 0.5 dB・Shift 0.1・Home・End）', async () => {
  const t = await byName('take');
  const vol = row(t).locator('.vol');
  await vol.focus();
  const head0 = await win.evaluate(() => window.__app.S.head);
  await win.keyboard.press('ArrowRight');
  expect((await byName('take')).gain_db).toBe(0.5);
  await expect(vol).toBeFocused();                                // 描き直してもフォーカスが残る
  await win.keyboard.press('ArrowRight');
  await win.keyboard.press('ArrowLeft');
  await win.keyboard.press('ArrowLeft');
  await win.keyboard.press('ArrowLeft');
  expect((await byName('take')).gain_db).toBe(-0.5);
  await win.keyboard.press('Shift+ArrowRight');
  expect((await byName('take')).gain_db).toBe(-0.4);
  await expect(vol).toHaveAttribute('aria-valuetext', '−0.4 dB');
  await win.keyboard.press('Home');
  expect((await byName('take')).gain_db).toBe(0);
  await win.keyboard.press('End');
  expect((await byName('take')).gain_db).toBe(-60);
  await expect(vol).toHaveAttribute('aria-valuetext', '−∞ dB');
  await expect(row(t).locator('.vv')).toHaveText('−∞');
  await win.keyboard.press('Home');
  // 上限
  for (let i = 0; i < 16; i++) await win.keyboard.press('ArrowRight');
  expect((await byName('take')).gain_db).toBe(6);
  await expect(row(t).locator('.vv')).toHaveText('+6.0');
  await win.keyboard.press('Home');
  expect((await byName('take')).gain_db).toBe(0);
  // ←→ は再生位置・ノートのコマンドに渡らない
  expect(await win.evaluate(() => window.__app.S.head)).toBe(head0);
  await expect.poll(async () => (await engineTrack(t.id)).gain_db, { timeout: 10000 }).toBe(0);
});

test('(X4) パン: 上下のドラッグ・ダブルクリックで中央・キー・読み上げの値', async () => {
  const t = await byName('vox2');
  const pan = row(t).locator('.pan');
  await drag(pan, { x: 8, y: 8 }, 0, -30, {
    mid: async () => {
      await expect(win.locator('#tvBub')).toHaveText(/^R \d+$/);
    },
  });
  await expect(win.locator('#tvBub')).toBeHidden();
  const p1 = (await byName('vox2')).pan;
  expect(p1).toBeCloseTo(0.3, 1);
  await expect(row(t).locator('.pan')).toHaveAttribute('aria-valuetext', /^右 \d+$/);
  await expect(row(t).locator('.pan')).toHaveAttribute('aria-valuenow', String(Math.round(p1 * 100)));
  await expect.poll(async () => (await engineTrack(t.id)).pan, { timeout: 10000 }).toBeCloseTo(p1, 3);
  // 下へ（左）
  await drag(row(t).locator('.pan'), { x: 8, y: 8 }, 0, 60);
  const p2 = (await byName('vox2')).pan;
  expect(p2).toBeCloseTo(p1 - 0.6, 1);
  await expect(row(t).locator('.pan')).toHaveAttribute('aria-valuetext', /^左 \d+$/);
  // 中央の近くは吸い付く
  await drag(row(t).locator('.pan'), { x: 8, y: 8 }, 0, -(0 - Math.round(p2 * 100)) - 1);
  expect((await byName('vox2')).pan).toBe(0);
  // ダブルクリックで中央
  await drag(row(t).locator('.pan'), { x: 8, y: 8 }, 0, -40);
  expect((await byName('vox2')).pan).toBeGreaterThan(0.3);
  const pb = await row(t).locator('.pan').boundingBox();
  await twice(pb.x + 8, pb.y + 8);
  expect((await byName('vox2')).pan).toBe(0);
  await expect(row(t).locator('.pan')).toHaveAttribute('aria-valuetext', '中央');
  // キー: 5、Shift で 1、Home で中央
  const kp = row(t).locator('.pan');
  await kp.focus();
  await win.keyboard.press('ArrowRight');
  expect((await byName('vox2')).pan).toBe(0.05);
  await win.keyboard.press('Shift+ArrowLeft');
  expect((await byName('vox2')).pan).toBe(0.04);
  await win.keyboard.press('ArrowUp');
  expect((await byName('vox2')).pan).toBe(0.09);
  await expect(kp).toBeFocused();
  for (let i = 0; i < 30; i++) await win.keyboard.press('ArrowLeft');
  expect((await byName('vox2')).pan).toBe(-1);
  await expect(kp).toHaveAttribute('aria-valuetext', '左 100');
  await win.keyboard.press('Home');
  expect((await byName('vox2')).pan).toBe(0);
  await expect.poll(async () => (await engineTrack(t.id)).pan, { timeout: 10000 }).toBe(0);
});

test('(X5) 聞こえないトラックは薄くなる（値は残る）', async () => {
  const t = await byName('Inst_mix');
  const op = (sel) => row(t).locator(sel).evaluate((el) => +getComputedStyle(el).opacity);
  expect(await op('.vol')).toBe(1);
  await row(t).locator('.vol').focus();
  await win.keyboard.press('Shift+ArrowLeft');                    // −0.1 dB
  await row(t).locator('button[data-act="m"]').click();
  await expect(row(t)).toHaveClass(/off/);
  expect(await op('.vol')).toBeCloseTo(0.45, 2);
  expect(await op('.pan')).toBeCloseTo(0.45, 2);
  expect(await op('.vv')).toBeCloseTo(0.45, 2);
  expect((await byName('Inst_mix')).gain_db).toBe(-0.1);
  // 薄くても触れば変えられる
  await row(t).locator('.vol').focus();
  await win.keyboard.press('Home');
  expect((await byName('Inst_mix')).gain_db).toBe(0);
  await row(t).locator('button[data-act="m"]').click();
  await expect(row(t)).not.toHaveClass(/off/);
  expect(await op('.vol')).toBe(1);
});

test('(X6) 取り消しの対象外で、session.json に保存される', async () => {
  const ts = await tracks();
  const t = ts.find((x) => x.name === 'vox2');
  // 取り消せる操作（名前）→ 音量・パン → Ctrl+Z は名前に届く
  await row(t).locator('.nm').click({ button: 'right' });
  await win.locator('#menu [data-cmd="rename"]').click();
  const input = win.locator('#heads input.rn');
  await input.fill('vox2b');
  await input.press('Enter');
  await settle();
  expect(await undoLabel()).toBe('トラックの名前');
  const t2 = await byName('vox2b');
  await row(t2).locator('.vol').focus();
  for (let i = 0; i < 6; i++) await win.keyboard.press('ArrowLeft');       // −3 dB
  await row(t2).locator('.pan').focus();
  await win.keyboard.press('ArrowRight');
  await win.keyboard.press('ArrowRight');                                   // R 10
  expect((await byName('vox2b')).gain_db).toBe(-3);
  expect((await byName('vox2b')).pan).toBe(0.1);
  expect(await undoLabel()).toBe('トラックの名前');                          // 履歴に入らない
  await expect.poll(async () => (await engineTrack(t.id)).gain_db, { timeout: 10000 }).toBe(-3);
  // session.json に入っている（作業場所の session.json）
  const sessionPath = (await win.evaluate(() => window.api.call('list_tracks', {}))).path;
  const saved = JSON.parse(fs.readFileSync(sessionPath, 'utf8')).tracks.find((x) => x.id === t.id);
  expect(saved.gain_db).toBe(-3);
  expect(saved.pan).toBe(0.1);
  expect(saved.name).toBe('vox2b');
  // Ctrl+Z は名前を戻し、音量・パンは今のまま
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await settle();
  const back = await byName('vox2');
  expect(back).toBeTruthy();
  expect(back.gain_db).toBe(-3);
  expect(back.pan).toBe(0.1);
  // やり直しても今の値
  await win.keyboard.press('Control+Shift+z');
  await settle();
  const again = await byName('vox2b');
  expect(again.gain_db).toBe(-3);
  expect(again.pan).toBe(0.1);
  // 元に戻す（以降のテストは vox2 の名前・0 dB・中央で進める）
  await win.keyboard.press('Control+z');
  await settle();
  const t3 = await byName('vox2');
  await row(t3).locator('.vol').focus();
  await win.keyboard.press('Home');
  await row(t3).locator('.pan').focus();
  await win.keyboard.press('Home');
  expect((await byName('vox2')).gain_db).toBe(0);
  expect((await byName('vox2')).pan).toBe(0);
});

test('(X7) 再生中に動かすと、その場で音量・パンが変わる', async () => {
  const ts = await tracks();
  await win.evaluate(() => { window.__app.S.head = 0; window.__app.S.loop = null; });
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 120000 });
  let ps = await win.evaluate(() => window.__app.playState());
  expect(ps.tracks.map((t) => [t.gain, t.pan])).toEqual([[1, 0], [1, 0], [1, 0]]);
  const t = ts.find((x) => x.name === 'vox2');
  await row(t).locator('.vol').focus();
  for (let i = 0; i < 12; i++) await win.keyboard.press('ArrowLeft');         // −6 dB
  await row(t).locator('.pan').focus();
  for (let i = 0; i < 4; i++) await win.keyboard.press('ArrowLeft');          // L 20
  ps = await win.evaluate(() => window.__app.playState());
  const p = ps.tracks.find((x) => x.id === t.id);
  expect(p.gain).toBeCloseTo(dbToGain(-6), 4);
  expect(p.pan).toBeCloseTo(-0.2, 4);
  expect(ps.tracks.filter((x) => x.id !== t.id).map((x) => [x.gain, x.pan])).toEqual([[1, 0], [1, 0]]);
  // ミュートは音量と掛け合わせる（ミュートすると 0、外すと音量に戻る）
  await row(t).locator('button[data-act="m"]').click();
  expect((await win.evaluate(() => window.__app.playState())).tracks.find((x) => x.id === t.id).gain).toBe(0);
  await row(t).locator('button[data-act="m"]').click();
  expect((await win.evaluate(() => window.__app.playState())).tracks.find((x) => x.id === t.id).gain).toBeCloseTo(dbToGain(-6), 4);
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => !window.__app.S.playing, null, { timeout: 10000 });
  // 次に再生を始めたときも、保存した値で鳴る
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 120000 });
  ps = await win.evaluate(() => window.__app.playState());
  const p2 = ps.tracks.find((x) => x.id === t.id);
  expect(p2.gain).toBeCloseTo(dbToGain(-6), 4);
  expect(p2.pan).toBeCloseTo(-0.2, 4);
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => !window.__app.S.playing, null, { timeout: 10000 });
  // 戻す
  await row(t).locator('.vol').focus();
  await win.keyboard.press('Home');
  await row(t).locator('.pan').focus();
  await win.keyboard.press('Home');
});

test('(X8) 見出しの幅: ドラッグ・ダブルクリック・キー・上限と下限・そろい・dB の数字', async () => {
  const s0 = await state();
  expect(s0.headW).toBe(160);
  const sep = win.locator('#hsz');
  await expect(sep).toHaveAttribute('role', 'separator');
  await expect(sep).toHaveCSS('cursor', 'col-resize');
  const widths = async () => ({
    heads: (await win.locator('#heads').boundingBox()).width,
    rhd: (await win.locator('.rhd').boundingBox()).width,
    lanesLeft: (await win.locator('#lanes').boundingBox()).x,
    rulerLeft: (await win.locator('#tvRuler').boundingBox()).x,
    headsLeft: (await win.locator('#heads').boundingBox()).x,
  });
  let w = await widths();
  expect(w.heads).toBeCloseTo(160, 0);
  // 境目の線は見出しの右端にある
  const sb = await sep.boundingBox();
  expect(Math.abs(sb.x + sb.width / 2 - (w.headsLeft + 160))).toBeLessThanOrEqual(1);
  // 右へ 60 px
  const cx = sb.x + sb.width / 2; const cy = sb.y + 30;
  const laneW0 = (await state()).laneW;
  await win.mouse.move(cx, cy);
  await win.mouse.down();
  await win.mouse.move(cx + 30, cy); await win.mouse.move(cx + 60, cy);
  await expect(win.locator('#tvBub')).toHaveText('220 px');
  await expect(sep).toHaveClass(/on/);
  await win.mouse.up();
  await expect(win.locator('#tvBub')).toBeHidden();
  expect((await state()).headW).toBe(220);
  w = await widths();
  expect(w.heads).toBeCloseTo(220, 0);
  expect(w.rhd).toBeCloseTo(220, 0);
  expect(w.lanesLeft).toBeCloseTo(w.headsLeft + 220, 0);          // レーン・ルーラーが見出しの右端から
  expect(w.rulerLeft).toBeCloseTo(w.lanesLeft, 0);
  expect((await state()).laneW).toBeCloseTo(laneW0 - 60, 0);      // レーンの幅はその分だけ狭くなる
  await expect(sep).toHaveAttribute('aria-valuenow', '220');
  // dB の数字は 150 px 以上で出る
  await expect(win.locator('#heads .vv').first()).toBeVisible();
  // 下限 140（左へ大きくドラッグ）・150 px 未満は dB の数字を隠す
  const sb2 = await sep.boundingBox();
  await win.mouse.move(sb2.x + 2, cy);
  await win.mouse.down();
  await win.mouse.move(sb2.x - 100, cy); await win.mouse.move(sb2.x - 400, cy);
  await win.mouse.up();
  expect((await state()).headW).toBe(140);
  await expect(win.locator('#heads .vv').first()).toBeHidden();
  await expect(win.locator('#heads .vol').first()).toBeVisible();
  // 名前（1 段目）と M・S・ガイドは収まる
  const rb = await row(await byName('take')).boundingBox();
  const mb = await row(await byName('take')).locator('button[data-act="s"]').boundingBox();
  expect(mb.x + mb.width).toBeLessThanOrEqual(rb.x + rb.width);
  // 上限 min(360, ビュー幅の 40%)
  const tvW = (await win.locator('#tv').boundingBox()).width;
  const max = Math.min(360, Math.floor(tvW * 0.4));
  await win.evaluate(() => window.__app.setHeadWidth(5000));
  expect((await state()).headW).toBe(max);
  const sb3 = await sep.boundingBox();
  await win.mouse.move(sb3.x + 2, cy);
  await win.mouse.down();
  await win.mouse.move(sb3.x + 300, cy); await win.mouse.move(sb3.x + 900, cy);
  await win.mouse.up();
  expect((await state()).headW).toBe(max);
  // キー ←→ 10 px
  await sep.focus();
  await win.keyboard.press('ArrowLeft');
  expect((await state()).headW).toBe(max - 10);
  await win.keyboard.press('ArrowRight');
  await win.keyboard.press('ArrowRight');
  expect((await state()).headW).toBe(max);
  // ダブルクリックで 160
  const sb4 = await sep.boundingBox();
  await win.mouse.dblclick(sb4.x + sb4.width / 2, cy);
  expect((await state()).headW).toBe(160);
  w = await widths();
  expect(w.heads).toBeCloseTo(160, 0);
  // 名前を変えるなどの描き直しを挟んでも、幅は変わらない
  await win.evaluate(() => window.__app.setHeadWidth(200));
  await win.evaluate(() => window.__app.renderTracks());
  expect((await widths()).heads).toBeCloseTo(200, 0);
});

test('(X9) 高さ 40 px 未満では 2 段目を畳み、値は名前のツールチップに出る', async () => {
  const t = await byName('take');
  await row(t).locator('.vol').focus();
  await win.keyboard.press('ArrowLeft');
  await win.evaluate(() => window.__app.setTrackHeight(28));
  await expect(row(t).locator('.r2')).toBeHidden();
  expect((await row(t).boundingBox()).height).toBeCloseTo(28, 0);
  await expect(row(t).locator('.nm')).toHaveAttribute('title', /音量 −0\.5 dB・パン C/);
  const r1 = await row(t).locator('.r1').boundingBox(); const rb = await row(t).boundingBox();
  expect(r1.y + r1.height).toBeLessThanOrEqual(rb.y + rb.height + 0.5);
  await win.evaluate(() => window.__app.setTrackHeight(44));
  await expect(row(t).locator('.r2')).toBeVisible();
  await row(t).locator('.vol').focus();
  await win.keyboard.press('Home');
});

test('(X10) 開き直しても残る（音量・パンはセッション、幅は表示の設定）', async () => {
  const t = await byName('take');
  const v = await byName('vox2');
  await row(t).locator('.vol').focus();
  for (let i = 0; i < 5; i++) await win.keyboard.press('ArrowLeft');                // −2.5 dB
  await row(v).locator('.pan').focus();
  for (let i = 0; i < 3; i++) await win.keyboard.press('ArrowRight');               // R 15
  await win.evaluate(() => window.__app.setHeadWidth(236));
  await expect.poll(async () => (await engineTrack(t.id)).gain_db, { timeout: 10000 }).toBe(-2.5);
  await expect.poll(async () => (await engineTrack(v.id)).pan, { timeout: 10000 }).toBe(0.15);
  // 表示の設定は 400 ms 遅れて書く
  await expect.poll(() => {
    const f = path.join(USERDATA, 'state.json');
    return fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, 'utf8')).view?.headW : null;
  }, { timeout: 10000 }).toBe(236);
  await app.close();
  await launch();
  await settle();
  await win.waitForFunction(() => window.__app.S.tracks.length === 3, null, { timeout: 60000 });
  const t2 = await byName('take');
  const v2 = await byName('vox2');
  expect(t2.gain_db).toBe(-2.5);
  expect(v2.pan).toBe(0.15);
  expect((await state()).headW).toBe(236);
  expect((await win.locator('#heads').boundingBox()).width).toBeCloseTo(236, 0);
  await expect(row(t2).locator('.vv')).toHaveText('−2.5');
  await expect(row(v2).locator('.pan')).toHaveAttribute('aria-valuetext', '右 15');
  // 開き直した後の再生にも効く
  await win.evaluate(() => { window.__app.S.head = 0; window.__app.S.loop = null; });
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 120000 });
  const ps = await win.evaluate(() => window.__app.playState());
  expect(ps.tracks.find((x) => x.id === t2.id).gain).toBeCloseTo(dbToGain(-2.5), 4);
  expect(ps.tracks.find((x) => x.id === v2.id).pan).toBeCloseTo(0.15, 4);
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => !window.__app.S.playing, null, { timeout: 10000 });
  void num;
});
