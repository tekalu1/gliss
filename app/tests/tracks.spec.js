// トラックビュー（上）＋エディタービュー（下）（issue #7。`docs/track-view.md`）。
//
//   再生（§2）:
//     (P1) テイク＋ガイド＋伴奏の 3 トラックを混ぜて鳴らす（ガイドも鳴る）。再生位置はタイムラインの秒で進む
//     (P2) ミュート／ソロは再生中でもその場で効く（トラックごとの音量）
//     (P3) 編集したテイクは編集を当てた音、他は元のファイルを鳴らす
//     (P4) ループは区間の頭に戻る（全トラック一緒に）
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2', 'E', 'B');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'projects', '_test-tracks');
const MEDIA = path.join(ROOT, 'Media');
const TAKE = path.join(MEDIA, 'take.wav');
const GUIDE = path.join(MEDIA, 'guide.wav');
const INST = path.join(MEDIA, 'Inst_mix.wav');            // 名前から伴奏として足される
const TAKE2 = path.join(MEDIA, 'take2.wav');
const PROJECT = path.join(ROOT, 'proj');
const USERDATA = path.join(ROOT, 'userdata');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  env.VOCAL_ENGINE_PROJECTS = path.join(ROOT, 'projects');
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(MEDIA, { recursive: true });
  fs.copyFileSync(M.clip('C'), TAKE);
  fs.copyFileSync(M.clip('C2'), GUIDE);
  fs.copyFileSync(M.clip('E'), INST);
  fs.copyFileSync(M.clip('B'), TAKE2);
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--guide', GUIDE, '--project-dir', PROJECT,
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
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null,
    { timeout: 240000 });
}

const tracks = () => win.evaluate(() => window.__app.tracks());
const byName = async (name) => (await tracks()).find((t) => t.name === name);
const playState = () => win.evaluate(() => window.__app.playState());

async function stopPlaying() {
  if (await win.evaluate(() => window.__app.S.playing)) {
    await win.locator('#bPlay').click();
    await win.waitForFunction(() => !window.__app.S.playing, null, { timeout: 10000 });
  }
}

test('(P1) テイク＋ガイド＋伴奏を混ぜて鳴らす（ガイドも鳴る）', async () => {
  await settle();
  await win.waitForFunction(() => window.__app.S.tracks.length === 2, null, { timeout: 60000 });
  // 伴奏を足す（名前から inst になる）
  await win.evaluate(async (p) => {
    await window.api.call('add_track', { path: p });
    await window.__app.loadSession();
  }, INST);
  const ts = await tracks();
  expect(ts.map((t) => [t.name, t.kind, t.guide, t.current])).toEqual([
    ['take', 'vocal', false, true],
    ['guide', 'vocal', true, false],
    ['Inst_mix', 'inst', false, false],
  ]);
  await win.evaluate(() => { window.__app.S.head = 0; window.__app.S.loop = null; });
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 120000 });
  const ps = await playState();
  expect(ps.tracks.map((t) => t.id)).toEqual(ts.map((t) => t.id));
  expect(ps.tracks.every((t) => t.gain === 1)).toBe(true);
  expect(ps.tracks.find((t) => t.id === ts[1].id).path).toBe(GUIDE);     // ガイドは元のファイル
  // 範囲はタイムライン全体（一番長いトラックの終わりまで）
  expect(ps.range[1]).toBeCloseTo(Math.max(...ts.map((t) => t.offset_sec + t.duration_sec)), 3);
  const h0 = await win.evaluate(() => window.__app.S.head);
  await win.waitForFunction((h) => window.__app.S.head > h + 0.3, h0, { timeout: 30000 });
});

test('(P2) ミュート／ソロは再生中でもその場で効く', async () => {
  const ts = await tracks();
  const [take, guide, inst] = ts;
  await win.evaluate((id) => window.__app.setTrack(id, { mute: true }), guide.id);
  let ps = await playState();
  expect(ps.tracks.map((t) => t.gain)).toEqual([1, 0, 1]);
  await win.evaluate((id) => window.__app.setTrack(id, { solo: true }), inst.id);
  ps = await playState();
  expect(ps.tracks.map((t) => t.gain)).toEqual([0, 0, 1]);
  // ソロはミュートより弱くない: ガイドをソロにしてもミュートのままなら鳴らない（DAW と同じ）
  await win.evaluate((id) => window.__app.setTrack(id, { solo: true }), guide.id);
  ps = await playState();
  expect(ps.tracks.map((t) => t.gain)).toEqual([0, 0, 1]);
  await win.evaluate(async ([g, i]) => {
    await window.__app.setTrack(g, { solo: false, mute: false });
    await window.__app.setTrack(i, { solo: false });
  }, [guide.id, inst.id]);
  ps = await playState();
  expect(ps.tracks.map((t) => t.gain)).toEqual([1, 1, 1]);
  // エンジン側（session.json）にも残っている
  const sj = JSON.parse(fs.readFileSync(path.join(PROJECT, 'session.json'), 'utf8'));
  expect(sj.tracks.map((t) => [t.mute, t.solo])).toEqual([[false, false], [false, false], [false, false]]);
  expect(take.current).toBe(true);
  await stopPlaying();
});

test('(P3) 編集したテイクは編集を当てた音、他は元のファイル', async () => {
  await win.evaluate(async () => {
    const n = window.__app.S.pitched[1];
    await window.api.call('shift_pitch', { note_id: n.id, cents: 100, author: 'human' });
    await window.__app.refresh();
  });
  await settle();
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 120000 });
  const ps = await playState();
  const [take, guide, inst] = ps.tracks;
  expect(take.edited).toBe(true);
  expect(path.basename(take.path)).toMatch(/^stem-[0-9a-f]+\.wav$/);
  expect(take.duration).toBeCloseTo(3.84, 2);             // 長さは元のまま
  expect(guide.edited).toBe(false);
  expect(inst.path).toBe(INST);
  await stopPlaying();
});

test('(P4) ループは区間の頭に戻る（全トラック一緒に）', async () => {
  await win.evaluate(() => { window.__app.S.loop = [0.5, 1.3]; window.__app.S.head = 0.5; });
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 120000 });
  const seen = await win.evaluate(() => new Promise((res) => {
    const out = [];
    const t0 = performance.now();
    const f = () => {
      out.push(window.__app.S.head);
      if (performance.now() - t0 < 1800) requestAnimationFrame(f); else res(out);
    };
    f();
  }));
  expect(Math.min(...seen)).toBeGreaterThanOrEqual(0.5 - 1e-6);
  expect(Math.max(...seen)).toBeLessThanOrEqual(1.3 + 1e-6);
  let wraps = 0;
  for (let i = 1; i < seen.length; i++) if (seen[i] < seen[i - 1] - 0.3) wraps++;
  expect(wraps).toBeGreaterThanOrEqual(1);
  await stopPlaying();
  await win.evaluate(() => { window.__app.S.loop = null; });
});

// ---------------------------------------------------------------- トラックビューの画面（§3）
const tstate = () => win.evaluate(() => window.__app.tracksState());
const lanesBox = () => win.locator('#lanes').boundingBox();
const view = () => win.evaluate(() => ({ ...window.__app.S.view, off: window.__app.S.off }));
const TH = 44;

/** トラックの行の中の y（ページ座標）。クリップの上半分 = 範囲・クリック、move = 下半分（位置をずらす）。 */
async function rowY(row, move = false) {
  const b = await lanesBox();
  return b.y + row * TH + (move ? 32 : 12);
}
async function xAt(t) {
  const b = await lanesBox();
  return b.x + await win.evaluate((tt) => window.__app.tvX(tt), t);
}

test('(V1) 上下に分割。見出しは 名前・ガイドのボタン・M・S、白枠は下の表示範囲', async () => {
  await settle();
  const st = await tstate();
  expect(st.height).toBe(st.fit);                          // 既定は全トラックが入る高さ
  expect(st.fit).toBe(20 + 3 * TH + 1);
  expect(st.heads.map((h) => [h.cur, h.guide])).toEqual([[true, false], [false, true], [false, false]]);
  // 伴奏にはガイドのボタンが無い。見出しのボタンは ガイド・M・S の 3 つだけ
  const btns = await win.evaluate(() => [...document.querySelectorAll('#heads .th')]
    .map((el) => [...el.querySelectorAll('button')].map((b) => b.dataset.act)));
  expect(btns).toEqual([['guide', 'm', 's'], ['guide', 'm', 's'], ['m', 's']]);
  // 白枠 = 下の表示範囲（編集中のトラックの行）
  await win.evaluate(() => { window.__app.setViewTimeline(1.0, 2.0); window.__app.render(); });
  const f = (await tstate()).frame;
  expect(f.row).toBe(0);
  const pps = st.laneW / (st.range[1] - st.range[0]);
  expect(f.x).toBeCloseTo(1.0 * pps, 0);
  expect(f.w).toBeCloseTo(1.0 * pps, 0);
});

test('(V2) クリップをクリック → そのトラックを編集対象にして、歌っているかたまりを下に出す', async () => {
  const x = await xAt(0.9);
  await win.mouse.click(x, await rowY(1));
  await win.waitForFunction((p) => window.__app.S.take?.path === p, GUIDE, { timeout: 120000 });
  await settle();
  const st = await tstate();
  expect(st.heads.map((h) => h.cur)).toEqual([false, true, false]);
  expect(st.frame.row).toBe(1);
  expect(await win.evaluate(() => window.__app.S.guide)).toBeNull();   // ガイド自身: 重ねない
  const want = await win.evaluate(() => {
    const t = window.__app.S.tracks[1];
    return window.__app.soundRegion(t, 0.9);
  });
  const v = await view();
  expect(v.t0 + v.off).toBeCloseTo(want[0], 2);
  expect(v.t0 + v.off + v.span).toBeCloseTo(want[1], 2);
  expect(want[1] - want[0]).toBeLessThan(3.84);           // クリップ全体ではなく、かたまり
});

test('(V3) トラック名のクリック → 表示範囲はそのまま、編集対象だけ切り替え', async () => {
  await win.evaluate(() => { window.__app.setViewTimeline(1.2, 2.4); window.__app.render(); });
  await win.locator('#heads .th[data-id] .nm').first().click();
  await win.waitForFunction((p) => window.__app.S.take?.path === p, TAKE, { timeout: 120000 });
  await settle();
  const v = await view();
  expect(v.t0 + v.off).toBeCloseTo(1.2, 3);
  expect(v.span).toBeCloseTo(1.2, 3);
  expect(await win.evaluate(() => window.__app.S.guide?.path)).toBe(GUIDE);   // ガイドが戻る
});

test('(V4) レーンを横にドラッグ → 下がその範囲にズーム（ドラッグ中も追従）', async () => {
  const y = await rowY(0);
  await win.mouse.move(await xAt(0.4), y);
  await win.mouse.down();
  await win.mouse.move(await xAt(1.0), y, { steps: 4 });
  let v = await view();
  expect(v.t0 + v.off).toBeCloseTo(0.4, 1);                // ドラッグ中も下が追従
  expect(v.span).toBeCloseTo(0.6, 1);
  await win.mouse.move(await xAt(2.2), y, { steps: 4 });
  await win.mouse.up();
  v = await view();
  expect(v.t0 + v.off).toBeCloseTo(0.4, 1);
  expect(v.span).toBeCloseTo(1.8, 1);
  const f = (await tstate()).frame;
  const st = await tstate();
  const pps = st.laneW / (st.range[1] - st.range[0]);
  expect(f.x).toBeCloseTo((v.t0 + v.off - st.range[0]) * pps, 0);
});

test('(V5) 伴奏のクリックは再生位置が動くだけ・ルーラーはクリックで再生位置、ドラッグでループ（上下共通）', async () => {
  const cur0 = await win.evaluate(() => window.__app.S.session.current);
  await win.mouse.click(await xAt(2.5), await rowY(2));
  expect(await win.evaluate(() => window.__app.S.head)).toBeCloseTo(2.5, 1);
  expect(await win.evaluate(() => window.__app.S.session.current)).toBe(cur0);
  // 上の再生位置の線と下の再生位置が同じ時刻
  const st = await tstate();
  expect(st.headX).toBeCloseTo(await win.evaluate(() => window.__app.tvX(window.__app.S.head)), 0);
  expect(st.clock).toBe(await win.locator('#clock').textContent());
  // 上のルーラーをドラッグ → ループ（下のピアノロールにも出る）
  const rb = await win.locator('#tvRuler').boundingBox();
  await win.mouse.move(await xAt(0.8), rb.y + 10);
  await win.mouse.down();
  await win.mouse.move(await xAt(1.6), rb.y + 10, { steps: 4 });
  await win.mouse.up();
  const loop = await win.evaluate(() => window.__app.S.loop);
  expect(loop[0]).toBeCloseTo(0.8, 1);
  expect(loop[1]).toBeCloseTo(1.6, 1);
  // 下のタイムスケールのクリック → 上の再生位置も同じところ（ループは外れる）
  const roll = await win.locator('#roll').boundingBox();
  await win.mouse.click(roll.x + roll.width * 0.5, roll.y + 10);
  const h = await win.evaluate(() => window.__app.S.head);
  expect(await win.evaluate(() => window.__app.S.loop)).toBeNull();
  expect((await tstate()).headX).toBeCloseTo(await win.evaluate((t) => window.__app.tvX(t), h), 0);
});

test('(V6) 共通のガイドはガイドのプルダウンの最後で 1 本だけ指定（もう一度で外れる）', async () => {
  const ts = await tracks();
  // テイクのガイドのボタン（プルダウン）の「このトラックを共通のガイドにする」→ 共通のガイドがテイクに移る
  // （編集中のトラック自身なので重ねない）。トラックごとのガイドは track-guide.spec.js
  const th0 = win.locator(`#heads .th[data-id="${ts[0].id}"]`);
  await th0.locator('.g').click();
  await expect(win.locator('#menu [data-item="guide"]')).toHaveText('このトラックを共通のガイドにする');
  await win.locator('#menu [data-item="guide"]').click();
  await win.waitForFunction((id) => window.__app.S.session.guide === id, ts[0].id);
  await settle();
  let st = await tstate();
  expect(st.heads.map((h) => h.guide)).toEqual([true, false, false]);
  expect(await win.evaluate(() => window.__app.S.guide)).toBeNull();
  // もう一度選ぶと外れる
  await th0.locator('.g').click();
  await expect(win.locator('#menu [data-item="guide"]')).toHaveText('共通のガイドから外す');
  await win.locator('#menu [data-item="guide"]').click();
  await win.waitForFunction(() => window.__app.S.session.guide === null);
  // 右クリックのメニューからも指定できる
  await win.locator(`#heads .th[data-id="${ts[1].id}"]`).click({ button: 'right' });
  await expect(win.locator('#menu [data-item="guide"]')).toHaveText('このトラックを共通のガイドにする');
  await win.locator('#menu [data-item="guide"]').click();
  await win.waitForFunction((id) => window.__app.S.session.guide === id, ts[1].id);
  await settle();
  st = await tstate();
  expect(st.heads.map((h) => h.guide)).toEqual([false, true, false]);
  expect(await win.evaluate(() => window.__app.S.guide?.path)).toBe(GUIDE);
});

test('(V7) M・S の見た目と、聞こえないトラックの見出しを暗く', async () => {
  const ts = await tracks();
  const th2 = win.locator(`#heads .th[data-id="${ts[2].id}"]`);
  await th2.locator('button[data-act="s"]').click();
  await expect(th2.locator('button[data-act="s"]')).toHaveAttribute('aria-pressed', 'true');
  const offs = async () => (await tstate()).heads.map((h) => h.off);
  await expect.poll(offs).toEqual([true, true, false]);
  await th2.locator('button[data-act="s"]').click();
  await expect.poll(offs).toEqual([false, false, false]);
  await win.locator(`#heads .th[data-id="${ts[1].id}"] button[data-act="m"]`).click();
  await expect.poll(offs).toEqual([false, true, false]);
  await win.locator(`#heads .th[data-id="${ts[1].id}"] button[data-act="m"]`).click();
  await expect.poll(offs).toEqual([false, false, false]);
});

test('(V8) 境界のドラッグで高さ、ダブルクリックで上を畳む／戻す', async () => {
  const h0 = (await tstate()).height;
  const sb = await win.locator('#split').boundingBox();
  await win.mouse.move(sb.x + 300, sb.y + 2);
  await win.mouse.down();
  await win.mouse.move(sb.x + 300, sb.y + 62, { steps: 4 });
  await win.mouse.up();
  // 画面の拡大率（Windows の表示スケール 150% など）では、ポインタの座標がデバイスのピクセルに丸められて
  // CSS の px で ±1/DPR ずれる（以前 1 度だけ落ちた原因。高さは整数に丸めるので 59 や 61 になりうる）
  expect(Math.abs((await tstate()).height - (h0 + 60))).toBeLessThanOrEqual(1);
  const sb2 = await win.locator('#split').boundingBox();
  await win.mouse.dblclick(sb2.x + 300, sb2.y + 2);
  expect((await tstate()).height).toBe(20 + TH);           // 1 トラック分に畳む
  const sb3 = await win.locator('#split').boundingBox();
  await win.mouse.dblclick(sb3.x + 300, sb3.y + 2);
  expect((await tstate()).height).toBe(h0);                // 全トラックが入る高さに戻す
});

test('(V9) スクリーンショット', async () => {
  await win.evaluate(() => {
    window.__app.S.loop = null;
    window.__app.S.head = 0.95;
    window.__app.setViewTimeline(0.3, 2.3);
    window.__app.render();
  });
  await win.waitForTimeout(300);
  await win.evaluate(() => window.api.screenshot('screenshot-tracks.png'));
});

// ---------------------------------------------------------------- 位置をずらす（§4）
const engineTracks = () => win.evaluate(() => window.api.call('list_tracks', {}));
const clipX = async (row) => (await tstate()).clips[row].x;
const pps = async () => { const st = await tstate(); return st.laneW / (st.range[1] - st.range[0]); };

/** クリップの上半分をつかんで dx px 動かす。途中（離す前）の見た目を返す。 */
async function dragClip(row, dx, { shift = false } = {}) {
  const x = await xAt(1.0);
  const y = await rowY(row, true);
  await win.mouse.move(x, y);
  await win.mouse.down();
  if (shift) await win.keyboard.down('Shift');
  await win.mouse.move(x + dx, y, { steps: 6 });
  const mid = await win.evaluate(() => ({
    st: window.__app.tracksState(), off: window.__app.S.off,
    preview: [...window.__app.S.trackOff.entries()],
    tip: document.querySelector('#tvTip text')?.textContent || null,
  }));
  if (shift) await win.keyboard.up('Shift');
  await win.mouse.up();
  await win.waitForFunction(() => window.__app.S.trackOff.size === 0, null, { timeout: 120000 });
  await settle();
  return mid;
}

function trefOf(file) {
  const b = fs.readFileSync(file);
  let i = 12;
  while (i + 8 <= b.length) {
    const id = b.toString('latin1', i, i + 4);
    const n = b.readUInt32LE(i + 4);
    if (id === 'bext') return Number(b.readBigUInt64LE(i + 8 + 338));
    i += 8 + n + (n & 1);
  }
  return null;
}

test('(M1) クリップの上半分を横にドラッグ → 音源全体の位置がずれる（ドラッグ中の見た目 = 離した後）', async () => {
  await win.evaluate(() => window.__app.selectTrack(window.__app.S.tracks[0].id));
  await settle();
  const x0 = await clipX(0);
  const k = await pps();
  const mid = await dragClip(0, 80);
  const [[id, v]] = mid.preview;
  expect(Math.abs(v * k - 80)).toBeLessThan(3);             // ポインタの座標は整数に丸まる
  // 描いている位置 = 見かけの位置（右端を越えたら、ドラッグ中に上の目盛りが広がる）
  const r = mid.st.range;
  expect(mid.st.clips[0].x).toBeCloseTo((v - r[0]) / (r[1] - r[0]) * mid.st.laneW, 0);
  expect(x0).toBe(0);
  expect(mid.off).toBeCloseTo(v, 6);                        // 下の目盛りもドラッグ中に動く
  expect(mid.tip).toMatch(/^\+0\.\d{3} s$/);
  // 離した後: エンジンの位置 = ドラッグ中の見かけ、クリップの位置も同じ
  const et = await engineTracks();
  expect(et.tracks.find((t) => t.id === id).offset_sec).toBeCloseTo(v, 6);
  expect(await clipX(0)).toBeCloseTo(mid.st.clips[0].x, 1);
  expect(await win.evaluate(() => window.__app.S.off)).toBeCloseTo(v, 6);
  // ガイドはタイムライン上の位置で切り出し直し、対応付け直した
  expect(et.guide_stale).toBe(false);
  expect(await win.evaluate(() => !!window.__app.S.vd.guide)).toBe(true);
  // 下の再生位置の線は、タイムラインの同じ秒（上と同じ）を指したまま
  const h = await win.evaluate(() => window.__app.S.head);
  expect((await tstate()).headX).toBeCloseTo(await win.evaluate((t) => window.__app.tvX(t), h), 0);
});

test('(M2) Ctrl+Z で元の位置に戻し、Ctrl+Shift+Z でやり直す', async () => {
  const id = (await tracks())[0].id;
  const v = (await tracks())[0].offset_sec;
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await expect.poll(async () => (await engineTracks()).tracks.find((t) => t.id === id).offset_sec)
    .toBe(0);
  await settle();
  expect(await win.evaluate(() => window.__app.S.off)).toBe(0);
  await win.keyboard.press('Control+Shift+z');
  await expect.poll(async () => (await engineTracks()).tracks.find((t) => t.id === id).offset_sec)
    .toBeCloseTo(v, 6);
  await settle();
});

test('(M3) 元の位置の近くに吸い付く・Shift で細かく', async () => {
  const k = await pps();
  const v = (await tracks())[0].offset_sec;
  // 0 から 3 px のところまで戻す → 0 に吸い付く
  const mid = await dragClip(0, -(v * k) + 3);
  expect(mid.preview[0][1]).toBe(0);
  expect(mid.tip).toBe('±0.000 s');
  expect((await tracks())[0].offset_sec).toBe(0);
  // Shift を押しながら 60 px → 6 px ぶん
  const mid2 = await dragClip(0, 60, { shift: true });
  expect(mid2.preview[0][1]).toBeGreaterThan(0);
  expect(mid2.preview[0][1] * k).toBeLessThan(9);
  expect((await tracks())[0].offset_sec).toBeCloseTo(mid2.preview[0][1], 6);
});

test('(M4) 書き出しは中身そのまま、DAW 上の位置（bext）をずらした量だけ動かす', async () => {
  const off = (await tracks())[0].offset_sec;
  expect(off).toBeGreaterThan(0);
  const r = await win.evaluate(() => window.__app.onMenu({ cmd: 'export' }));
  await settle();
  expect(r.timeline_offset_sec).toBeCloseTo(off, 6);
  expect(trefOf(r.path)).toBe(Math.round(off * r.sr));      // 元に bext が無くても入れる
  expect(await win.evaluate(() => window.__app.status())).toContain('秒ずらした');
});

test('(M5) ガイドのトラックをずらす → 編集中のテイクのガイドを切り出し直す・伴奏をずらしても編集対象は開き直さない', async () => {
  const x1 = await clipX(1);
  await dragClip(1, 40);
  expect(Math.abs(await clipX(1) - (x1 + 40))).toBeLessThan(3);
  const et = await engineTracks();
  expect(et.tracks[1].offset_sec).toBeGreaterThan(0);
  expect(et.guide_stale).toBe(false);
  expect(await win.evaluate(() => !!window.__app.S.vd.guide)).toBe(true);
  const cur = await win.evaluate(() => window.__app.S.session.current);
  const x2 = await clipX(2);
  await dragClip(2, 30);
  expect(Math.abs(await clipX(2) - (x2 + 30))).toBeLessThan(3);
  expect(await win.evaluate(() => window.__app.S.session.current)).toBe(cur);
  // 再生すると、ずらした位置で鳴る
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 120000 });
  const ps = await playState();
  const now = (await engineTracks()).tracks;
  expect(ps.tracks.map((t) => t.start)).toEqual(now.map((t) => t.offset_sec));
  await stopPlaying();
  await win.evaluate(() => window.api.screenshot('screenshot-tracks-move.png'));
});

test('(M6) 下半分でも動かさずに離せばクリック・右クリックの「元の位置に戻す」', async () => {
  const ts = await tracks();
  // ガイドの行の下半分をクリック（動かさない）→ そのトラックに切り替わる
  await win.mouse.click(await xAt(ts[1].offset_sec + 0.9), await rowY(1, true));
  await win.waitForFunction((p) => window.__app.S.take?.path === p, GUIDE, { timeout: 120000 });
  await settle();
  expect((await engineTracks()).tracks[1].offset_sec).toBe(ts[1].offset_sec);   // 位置は変わらない
  await win.locator('#heads .th[data-id] .nm').first().click();
  await win.waitForFunction((p) => window.__app.S.take?.path === p, TAKE, { timeout: 120000 });
  await settle();
  // 右クリック → 元の位置に戻す
  await win.locator(`#heads .th[data-id="${ts[1].id}"] .nm`).click({ button: 'right' });
  await expect(win.locator('#menu [data-item="zero"]')).toBeEnabled();
  await win.locator('#menu [data-item="zero"]').click();
  await expect.poll(async () => (await engineTracks()).tracks[1].offset_sec).toBe(0);
  await settle();
  // Esc でメニューを閉じても、ノートの選択は外れない
  await win.evaluate(() => { window.__app.S.sel = [window.__app.S.pitched[0].id]; });
  await win.locator(`#heads .th[data-id="${ts[1].id}"] .nm`).click({ button: 'right' });
  await expect(win.locator('#menu [data-item="zero"]')).toBeDisabled();
  await win.keyboard.press('Escape');
  await expect(win.locator('#menu')).toBeHidden();
  expect(await win.evaluate(() => window.__app.S.sel.length)).toBe(1);
  await win.evaluate(() => { window.__app.S.sel = []; window.__app.render(); });
});

/** ボタンを押していない OS のマウスの動き（Playwright の CDP ではなく、実マウスと同じ経路）。 */
const osMouseMove = (x, y) => app.evaluate(({ BrowserWindow }, [px, py]) => {
  BrowserWindow.getAllWindows()[0].webContents.sendInputEvent({ type: 'mouseMove', x: px, y: py });
}, [Math.round(x), Math.round(y)]);

test('(M7) ずらしている間に OS のマウスが割り込み、離したことが届かなくても、見せていた位置で当たる', async () => {
  // connection.spec (8) と同じ（state.js の buttonReleased）。キャプチャが外れた後の move はボタンを
  // 押していない。以前はドラッグが残り、離した後もクリップがマウスについて行った
  const id = (await tracks())[0].id;
  const off0 = (await tracks())[0].offset_sec;
  const engineOff = async () => (await engineTracks()).tracks.find((t) => t.id === id).offset_sec;
  const x = await xAt(1.0);
  const y = await rowY(0, true);
  await win.mouse.move(x, y);
  await win.mouse.down();
  await win.mouse.move(x + 60, y, { steps: 6 });
  const v = await win.evaluate((tid) => window.__app.S.trackOff.get(tid), id);
  expect(Math.abs(v - off0)).toBeGreaterThan(0.01);
  await osMouseMove(x + 300, y);
  await expect.poll(engineOff).toBeCloseTo(v, 6);       // そこで離したことにして、見せていた位置で当たる
  await win.mouse.up();                                 // 遅れて届いた pointerup は何もしない
  await osMouseMove(x + 200, y);                        // ボタンを押さずに上を通る
  await osMouseMove(x + 100, y);
  await win.waitForFunction(() => window.__app.S.trackOff.size === 0, null, { timeout: 120000 });
  await settle();
  expect(await engineOff()).toBeCloseTo(v, 6);
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await expect.poll(engineOff).toBeCloseTo(off0, 6);
  await settle();
});

// ---------------------------------------------------------------- トラックの追加と削除（§5）
const KARAOKE = path.join(MEDIA, 'song_karaoke.wav');     // 名前から伴奏

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
    const target = document.querySelector('#lanes');
    const init = { bubbles: true, cancelable: true, dataTransfer: dt, shiftKey: sh };
    target.dispatchEvent(new DragEvent('dragenter', init));
    target.dispatchEvent(new DragEvent('dragover', init));
    const hint = document.querySelector('#status').textContent;
    target.dispatchEvent(new DragEvent('drop', init));
    return hint;
  }, shift);
}

async function rightClickHead(id) {
  await win.locator(`#heads .th[data-id="${id}"] .nm`).click({ button: 'right' });
  await expect(win.locator('#menu')).toBeVisible();
}

test('(A1) ファイル > トラックを追加… で足す（編集対象は変えない）', async () => {
  fs.copyFileSync(M.clip('E'), KARAOKE);
  await app.evaluate(async ({ dialog }, p) => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [p] });
  }, TAKE2);
  const cur = await win.evaluate(() => window.__app.S.session.current);
  await win.evaluate(() => window.__app.onMenu({ cmd: 'add-track' }));
  await settle();
  const ts = await tracks();
  expect(ts.map((t) => [t.name, t.kind])).toEqual([
    ['take', 'vocal'], ['guide', 'vocal'], ['Inst_mix', 'inst'],
    ['take2', 'vocal']]);
  expect(await win.evaluate(() => window.__app.S.session.current)).toBe(cur);
  const st = await tstate();
  expect(st.height).toBe(Math.min(st.fit, st.height));     // 全トラックが入る高さ（40% まで）
  expect(st.fit).toBe(20 + 4 * TH + 1);
  await expect.poll(async () => (await tstate()).overviews).toBe(4);
});

test('(A2) ドロップ: 既にあるテイクは切り替え、新しい伴奏は足すだけ', async () => {
  const hint = await drop(TAKE2);
  expect(hint).toContain('トラックに足して');
  await win.waitForFunction((p) => window.__app.S.take?.path === p, TAKE2, { timeout: 120000 });
  await settle();
  expect((await tracks()).length).toBe(4);
  const cur = await win.evaluate(() => window.__app.S.session.current);
  await drop(KARAOKE);
  await expect.poll(async () => (await tracks()).length).toBe(5);
  await settle();
  const k = (await tracks())[4];
  expect([k.name, k.kind]).toEqual(['song_karaoke', 'inst']);
  expect(await win.evaluate(() => window.__app.S.session.current)).toBe(cur);   // 伴奏は選ばない
  expect(await win.evaluate(() => window.__app.status())).toContain('伴奏');
  // Shift で落とすとガイドとして（もうガイドのファイルなら何もしない）
  await drop(GUIDE, { shift: true });
  await expect.poll(() => win.evaluate(() => window.__app.status())).toContain('もうガイドとして開いている');
});

test('(A3) 右クリックのメニュー: 伴奏として扱う／ボーカルとして扱う', async () => {
  const ts = await tracks();
  const cur = ts.find((t) => t.current);
  await rightClickHead(cur.id);
  await expect(win.locator('#menu [data-item="kind"]')).toBeDisabled();     // 編集中のトラックは伴奏にできない
  await win.keyboard.press('Escape');
  await expect(win.locator('#menu')).toBeHidden();
  const k = ts[4];
  await rightClickHead(k.id);
  await expect(win.locator('#menu [data-item="kind"]')).toHaveText('ボーカルとして扱う');
  await expect(win.locator('#menu [data-item="guide"]')).toBeHidden();      // 伴奏はガイドにできない
  await win.locator('#menu [data-item="kind"]').click();
  await expect.poll(async () => (await tracks())[4].kind).toBe('vocal');
  await settle();
  const btns = await win.evaluate(() => [...document.querySelectorAll('#heads .th')][4]
    .querySelectorAll('button').length);
  expect(btns).toBe(3);                                     // ボーカルになったのでガイドのアイコンが出る
  await rightClickHead(k.id);
  await win.locator('#menu [data-item="kind"]').click();
  await expect.poll(async () => (await tracks())[4].kind).toBe('inst');
  await settle();
});

test('(A4) トラックを外す（編集中なら残りの最初のボーカルへ。ファイルと編集は消さない）', async () => {
  let ts = await tracks();
  const cur = ts.find((t) => t.current);
  const dir = cur.project_dir;
  await rightClickHead(cur.id);
  await win.locator('#menu [data-item="remove"]').click();
  await expect.poll(async () => (await tracks()).length).toBe(4);
  await settle();
  ts = await tracks();
  expect(ts.find((t) => t.current).name).toBe('take');
  expect(await win.evaluate(() => window.__app.S.take?.path)).toBe(TAKE);
  expect(fs.existsSync(path.join(dir, 'project.json'))).toBe(true);
  expect(fs.existsSync(TAKE2)).toBe(true);
  // 伴奏も外せる
  await rightClickHead(ts[3].id);
  await win.locator('#menu [data-item="remove"]').click();
  await expect.poll(async () => (await tracks()).length).toBe(3);
  await settle();
  expect((await tstate()).height).toBe(20 + 3 * TH + 1);    // 高さは入るぶんに戻る
  // 外したファイルを Shift で落とすと、トラックに足し直してガイドに指定する（編集中のテイクはそのまま）
  await drop(TAKE2, { shift: true });
  await win.waitForFunction((p) => window.__app.S.guide?.path === p, TAKE2, { timeout: 120000 });
  await settle();
  expect(await win.evaluate(() => window.__app.S.take?.path)).toBe(TAKE);
  expect((await tracks()).find((t) => t.guide).name).toBe('take2');
  await win.evaluate(() => window.api.screenshot('screenshot-tracks-add.png'));
});

test('実行時のコンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});
