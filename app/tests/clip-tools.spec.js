// トラックビュー（上）のはさみ・ミュート（承認済み 2026-10-03。`docs/track-view.md` §8）。音声は合成（素材は要らない）。
//
//   (T1) ツールを替えると上のカーソルとステータス行の説明が替わる（はさみ・ミュートはクリップの上だけ。鉛筆は矢印）
//   (T2) はさみ: クリックで切る（ホバーで縦線）・切れ目のダブルクリックでつなぐ・端に近すぎれば断る
//   (T3) ミュート: 部分のクリックで消す⇔戻す。消した部分は点線の輪郭・下のピアノロールに斜線の帯
//   (T4) なぞってまとめて（行をまたぐ）。Ctrl+Z 1 回で全部戻り、やり直しで戻る。保存される（session.json）
//   (T5) 再生: 消した区間は鳴らさない（伴奏にも効く）。再生中に消しても当たる
//   (T6) メイン・鉛筆は今までどおり（クリックで下に出す・範囲）。位置をずらすと切れ目・消した部分も一緒に動く
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SHOTS = process.env.GLISS_SCREENSHOT_DIR || path.join(os.tmpdir(), 'gliss-clip-tools-shots');
const SR = 44100;
const NOTES = [[0.4, 1.0, 220], [1.25, 1.85, 247], [2.1, 2.7, 277], [2.95, 3.55, 330]];

function wavHeader(n) {
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(SR, 24); buf.writeUInt32LE(SR * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  return buf;
}
function writeTones(file) {
  const n = Math.round(SR * 4.0);
  const buf = wavHeader(n);
  for (let i = 0; i < n; i++) {
    const t = i / SR;
    const nt = NOTES.find(([a, b]) => t >= a && t < b);
    let v = 0;
    if (nt) {
      const env = Math.min(1, (t - nt[0]) / 0.03, (nt[1] - t) / 0.03);
      for (let k = 1; k <= 6; k++) v += Math.sin(2 * Math.PI * nt[2] * k * t) / k;
      v *= 0.25 * env;
    }
    buf.writeInt16LE(Math.round(v * 32767), 44 + i * 2);
  }
  fs.writeFileSync(file, buf);
}
function writePad(file) {
  const n = Math.round(SR * 4.0);
  const buf = wavHeader(n);
  for (let i = 0; i < n; i++) {
    const t = i / SR;
    const v = 0.2 * (Math.sin(2 * Math.PI * 110 * t) + 0.5 * Math.sin(2 * Math.PI * 165 * t)) * (0.6 + 0.4 * Math.sin(2 * Math.PI * 2 * t));
    buf.writeInt16LE(Math.round(v * 32767), 44 + i * 2);
  }
  fs.writeFileSync(file, buf);
}

let app;
let win;
let dir;
let take;
let inst;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-clip-spec-'));
  const wav = path.join(dir, 'tones.wav');
  writeTones(wav);
  const padPath = path.join(dir, 'Inst_pad.wav');           // 名前から伴奏として足される
  writePad(padPath);
  fs.mkdirSync(SHOTS, { recursive: true });
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_EDITOR_MUTE: '1',
    VOCAL_ENGINE_AUTO_LYRICS: '0', VOCAL_ENGINE_WORK_DIR: path.join(dir, 'work'),
    VOCAL_ENGINE_PROJECTS: path.join(dir, 'projects') };
  delete env.ELECTRON_RUN_AS_NODE;
  app = await electron.launch({
    args: [APP, wav, '--project-dir', path.join(dir, 'project'), '--user-data-dir', path.join(dir, 'userdata'), '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await win.waitForFunction(() => window.__app.notes().length >= 4, null, { timeout: 120000 });
  await win.evaluate(async (p) => {
    await window.api.call('add_track', { path: p, author: 'human' });
    await window.__app.loadSession();
  }, padPath);
  const ts = await tracks();
  take = ts.find((t) => t.kind === 'vocal');
  inst = ts.find((t) => t.kind === 'inst');
});

test.afterAll(async () => {
  await app?.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

const settle = () => win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 120000 });
const tracks = () => win.evaluate(() => window.__app.tracks());
const trk = async (id) => (await tracks()).find((t) => t.id === id);
const status = () => win.evaluate(() => window.__app.status());
const shot = (name) => win.screenshot({ path: path.join(SHOTS, `${name}.png`) });
const engineTrack = (id) => win.evaluate(async (i) => (await window.api.call('list_tracks', {})).tracks.find((t) => t.id === i), id);

/** トラック id のクリップの上の点（画面の座標）。sec = タイムラインの秒、frac = クリップの高さの何割か（0〜1）。 */
async function at(id, sec, frac = 0.3) {
  return win.evaluate(([i, s, f]) => {
    const r = document.querySelector('#lanes').getBoundingClientRect();
    const st = window.__app.tracksState();
    const row = st.clips.find((c) => c.id === i).row;
    return { x: r.left + window.__app.tvX(s), y: r.top + row * st.trackH + 4 + (st.trackH - 7) * f };
  }, [id, sec, frac]);
}
async function click(id, sec, frac, opts) {
  const p = await at(id, sec, frac);
  await win.mouse.click(p.x, p.y, opts);
  await settle();
}
async function tool(n) {
  await win.locator('#mock').focus();
  await win.keyboard.press(String(n));
}
async function undoOnce() {
  await win.evaluate(() => window.__app.onMenu({ cmd: 'undo' }));
  await settle();
}
async function redoOnce() {
  await win.evaluate(() => window.__app.onMenu({ cmd: 'redo' }));
  await settle();
}
async function resetClips() {
  for (const t of await tracks()) {
    await win.evaluate(async (id) => {
      const r = await window.api.call('list_tracks', {});
      const tr = r.tracks.find((x) => x.id === id);
      for (const c of [...tr.cuts]) await window.api.call('join_track', { track_id: id, sec: c, author: 'human' });
      if (tr.mutes.length) await window.api.call('mute_track_range', { track_id: id, start_sec: 0, end_sec: tr.duration_sec, mute: false, author: 'human' });
      await window.__app.loadSession();
    }, t.id);
  }
  await settle();
}

test('(T1) ツールを替えると上のカーソルとステータス行の説明が替わる', async () => {
  await tool(3);
  expect(await status()).toContain('はさみ');
  expect(await status()).toContain('上はクリップをクリックで分ける');
  const p = await at(take.id, 2.0);
  await win.mouse.move(p.x, p.y);
  expect(await win.locator('#lanes').evaluate((el) => el.classList.contains('cur-cut'))).toBe(true);
  expect(await win.evaluate(() => getComputedStyle(document.querySelector('#lanes')).cursor)).toContain('svg');
  // クリップの外（伴奏の右の空き）は矢印
  const out = await at(take.id, 4.12);
  await win.mouse.move(out.x, out.y);
  expect(await win.locator('#lanes').evaluate((el) => el.classList.contains('cur-cut'))).toBe(false);
  await tool(4);
  expect(await status()).toContain('ミュート');
  await win.mouse.move(p.x, p.y + 1);
  expect(await win.locator('#lanes').evaluate((el) => el.classList.contains('cur-mute'))).toBe(true);
  expect(await win.evaluate(() => getComputedStyle(document.querySelector('#lanes')).cursor)).toContain('svg');
  await tool(2);                                    // 鉛筆: 上はメインと同じに働く（カーソルは矢印）
  expect(await status()).toContain('メインと同じ');
  await win.mouse.move(p.x, p.y + 2);
  expect(await win.evaluate(() => getComputedStyle(document.querySelector('#lanes')).cursor)).toBe('default');
  await tool(1);
  expect(await status()).toContain('メインツール');
  await win.mouse.move(p.x, p.y + 20);              // メイン: 今までどおり（下半分は位置をずらす手）
  expect(await win.evaluate(() => getComputedStyle(document.querySelector('#lanes')).cursor)).toBe('grab');
});

test('(T2) はさみ: クリックで切る・ホバーで縦線・切れ目のダブルクリックでつなぐ', async () => {
  await tool(3);
  const p = await at(take.id, 2.0);
  await win.mouse.move(p.x, p.y);
  await expect(win.locator('#lanes #tvcut')).toHaveCount(1);          // ホバーの縦線
  await win.mouse.click(p.x, p.y);
  await settle();
  let t = await trk(take.id);
  expect(t.cuts).toHaveLength(1);
  expect(t.cuts[0]).toBeGreaterThan(1.9);
  expect(t.cuts[0]).toBeLessThan(2.1);
  expect((await engineTrack(take.id)).cuts).toEqual(t.cuts);          // エンジンにも入った
  expect(await status()).toContain('で分けた');
  expect(await win.locator(`#lanes line[data-cut="${take.id}"]`).count()).toBe(1);
  // 伴奏も切れる
  await click(inst.id, 1.0, 0.3);
  expect((await trk(inst.id)).cuts).toHaveLength(1);
  // 切れ目の上のシングルクリックは何もしない（増やさない）。ダブルクリックでつなぐ
  const c = await at(take.id, t.cuts[0] + (await trk(take.id)).offset_sec);
  await win.mouse.move(c.x, c.y);
  await win.mouse.click(c.x, c.y);
  await settle();
  expect((await trk(take.id)).cuts).toHaveLength(1);
  await win.waitForTimeout(600);                                     // ダブルクリックの間隔（450 ms）を空ける
  await win.mouse.dblclick(c.x, c.y);
  await settle();
  expect((await trk(take.id)).cuts).toHaveLength(0);
  expect((await engineTrack(take.id)).cuts).toEqual([]);
  expect(await status()).toContain('つないだ');
  // 端に近すぎる
  const e = await at(take.id, 0.005);
  await win.mouse.click(e.x, e.y);
  await settle();
  expect((await trk(take.id)).cuts).toHaveLength(0);
  expect(await status()).toContain('端に近すぎる');
  await resetClips();
});

test('(T3) ミュート: 部分のクリックで消す⇔戻す。点線の輪郭・下に斜線の帯', async () => {
  await tool(3);
  await click(take.id, 1.0, 0.3);
  await click(take.id, 3.0, 0.3);
  const cuts = (await trk(take.id)).cuts;
  expect(cuts).toHaveLength(2);
  await tool(4);
  await click(take.id, 2.0, 0.3);                                     // 真ん中の部分
  let t = await trk(take.id);
  expect(t.mutes).toHaveLength(1);
  expect(t.mutes[0][0]).toBeCloseTo(cuts[0], 3);
  expect(t.mutes[0][1]).toBeCloseTo(cuts[1], 3);
  expect(await status()).toContain('消した');
  expect(await win.locator(`#lanes rect[data-mute-range="${take.id}"]`).count()).toBe(1);
  // 下のピアノロールに「クリップで消している」斜線の帯
  expect(await win.locator('#roll [data-clip-mute]').count()).toBe(1);
  await shot('t3-muted');
  await click(take.id, 2.0, 0.3);                                     // もう一度で戻る
  expect((await trk(take.id)).mutes).toEqual([]);
  expect(await win.locator('#roll [data-clip-mute]').count()).toBe(0);
  // ノートの無音（下のミュートツール）とは別: ノートは無音になっていない
  expect((await win.evaluate(() => window.__app.notes())).filter((n) => n.muted)).toHaveLength(0);
  await resetClips();
});

test('(T4) なぞってまとめて・Ctrl+Z 1 回で戻る・やり直し・保存される', async () => {
  await tool(3);
  await click(take.id, 1.2, 0.3);
  await click(take.id, 2.4, 0.3);
  await click(inst.id, 2.0, 0.3);
  await tool(4);
  // ボーカルの左の部分から 伴奏の中の部分 まで斜めになぞる（行をまたぐ）
  const a = await at(take.id, 0.6);
  const b = await at(take.id, 3.0);
  const c = await at(inst.id, 3.0);
  await win.mouse.move(a.x, a.y);
  await win.mouse.down();
  await win.mouse.move(b.x, b.y, { steps: 8 });
  await win.mouse.move(c.x, c.y, { steps: 8 });
  await win.mouse.up();
  await settle();
  const tk = await trk(take.id);
  const ins = await trk(inst.id);
  expect(tk.mutes).toEqual([[0, tk.duration_sec]]);                    // 3 つの部分が 1 つの区間にまとまる
  expect(ins.mutes.length).toBe(1);
  expect(await status()).toContain('か所を消した');
  // session.json にも入っている
  const sj = JSON.parse(fs.readFileSync(path.join(dir, 'project', 'session.json'), 'utf8'));
  expect(sj.tracks.find((t) => t.id === take.id).mutes).toEqual(tk.mutes);
  expect(sj.version).toBe(1);
  // Ctrl+Z 1 回で、なぞった分が全部戻る（直前のはさみは残る）
  await undoOnce();
  expect((await trk(take.id)).mutes).toEqual([]);
  expect((await trk(inst.id)).mutes).toEqual([]);
  expect((await trk(take.id)).cuts).toHaveLength(2);
  const h = await win.evaluate(() => window.__app.hist());
  expect(h.redo.label).toBe('部分のミュート');
  await redoOnce();
  expect((await trk(take.id)).mutes).toEqual(tk.mutes);
  expect((await trk(inst.id)).mutes).toEqual(ins.mutes);
  // はさみも Ctrl+Z で戻る
  await undoOnce();
  await undoOnce();
  expect((await trk(inst.id)).cuts).toEqual([]);
  expect(await win.evaluate(() => window.__app.hist().undo.label)).toBe('クリップを分ける');
  await resetClips();
});

test('(T5) 再生: 消した区間は鳴らさない（伴奏にも効く。再生中に消しても当たる）', async () => {
  await tool(3);
  await click(take.id, 1.0, 0.3);
  await click(take.id, 2.0, 0.3);
  await click(inst.id, 1.0, 0.3);
  await click(inst.id, 2.0, 0.3);
  await tool(4);
  await click(take.id, 1.5, 0.3);
  await click(inst.id, 1.5, 0.3);
  const ts = await tracks();
  const m = (await trk(take.id)).mutes[0];
  expect(m).toBeTruthy();
  await win.evaluate((h) => { window.__app.S.loop = null; window.__app.S.head = h; }, m[0] - 0.12);
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 120000 });
  let ps = await win.evaluate(() => window.__app.playState());
  expect(ps.tracks.find((t) => t.id === take.id).muted).toEqual([[m[0], m[1]]]);
  expect(ps.tracks.find((t) => t.id === inst.id).muted).toHaveLength(1);
  // 区間の中では 0、区間の外では 1（ミュート／ソロの gain は 1 のまま）
  const sample = (id) => win.evaluate((i) => {
    const s = window.__app.playState();
    return { head: window.__app.S.head, g: s.tracks.find((t) => t.id === i).clipGain, ms: s.tracks.find((t) => t.id === i).gain };
  }, id);
  await win.waitForFunction((a) => window.__app.S.head > a + 0.1, m[0], { timeout: 30000 });
  let s = await sample(take.id);
  expect(s.head).toBeLessThan(m[1]);
  expect(s.g).toBeLessThan(0.01);
  expect(s.ms).toBe(1);
  expect((await sample(inst.id)).g).toBeLessThan(0.01);
  await win.waitForFunction((b) => window.__app.S.head > b + 0.1, m[1], { timeout: 30000 });
  s = await sample(take.id);
  expect(s.g).toBeGreaterThan(0.99);
  // 再生中にもう 1 か所を消す → 今から先の音にも当たる（止めて鳴らし直さない）
  await click(take.id, 2.6, 0.3);                        // 後ろの部分（消した部分につながって 1 つの区間になる）
  ps = await win.evaluate(() => window.__app.playState());
  const live = ps.tracks.find((t) => t.id === take.id).muted;
  expect(live).toHaveLength(1);
  expect(live[0][0]).toBeCloseTo(m[0], 3);
  expect(live[0][1]).toBeGreaterThan(m[1] + 1);
  await win.waitForFunction((id) => window.__app.playState().tracks.find((t) => t.id === id).clipGain < 0.01, take.id, { timeout: 5000 });
  expect(await win.evaluate(() => window.__app.S.playing)).toBe(true);
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => !window.__app.S.playing, null, { timeout: 10000 });
  expect(ts.length).toBe(2);
  await resetClips();
});

test('(T6) メイン・鉛筆は今までどおり。位置をずらすと切れ目・消した部分も一緒に動く', async () => {
  await tool(3);
  await click(take.id, 2.0, 0.3);
  await tool(4);
  await click(take.id, 3.0, 0.3);
  const before = await trk(take.id);
  expect(before.mutes).toHaveLength(1);
  // メインに戻す: クリップの上半分のクリックは「下に出す」（切れ目・消した部分は変わらない）
  await tool(1);
  await click(take.id, 1.0, 0.2);
  expect((await trk(take.id)).cuts).toEqual(before.cuts);
  expect((await trk(take.id)).mutes).toEqual(before.mutes);
  // 鉛筆でも同じ
  await tool(2);
  await click(take.id, 1.2, 0.2);
  expect((await trk(take.id)).cuts).toEqual(before.cuts);
  // 位置をずらす（エンジン）: 切れ目・消した部分の値は同じ（トラックの頭が 0 の秒なので、一緒に動く）
  await win.evaluate(async (id) => { await window.api.call('set_track', { track_id: id, offset_sec: 0.5, author: 'human' }); await window.__app.loadSession(); }, take.id);
  await settle();
  const moved = await trk(take.id);
  expect(moved.offset_sec).toBeCloseTo(0.5, 6);
  expect(moved.cuts).toEqual(before.cuts);
  expect(moved.mutes).toEqual(before.mutes);
  // 画面の上でも、消した部分の枠がクリップと一緒に 0.5 秒ぶんずれて描かれている
  const pos = await win.evaluate(([id, off]) => {
    const r = document.querySelector(`#lanes rect[data-mute-range="${id}"]`);
    return { x: +r.getAttribute('x'), want: window.__app.tvX(off) };
  }, [take.id, 0.5 + before.mutes[0][0]]);
  expect(pos.x).toBeCloseTo(pos.want, 0);
  await win.evaluate(async (id) => { await window.api.call('set_track', { track_id: id, offset_sec: 0, author: 'human' }); await window.__app.loadSession(); }, take.id);
  await tool(1);
  await resetClips();
  expect(errors).toEqual([]);
});
