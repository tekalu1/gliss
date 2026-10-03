// プラグイン（VST3 + ARA 2。DAW のエディタ欄の WebView2）の画面のモード（renderer/ara.js・ara-bridge.js）。
// Electron の画面を環境変数 GLISS_TEST_ARA=1 で「プラグインのモード」にして見る（preload が window.api.mode = 'ara'・偽の DAW の再生の制御・
// C++ のイベントを差し込む口 __araEmit を出す）。エンジンは本物で、音声は合成（素材・解析モデルの重みは要らない）。
//
//   (A1) 起動: html に data-mode="ara"。ロゴ・更新の知らせ・初回の画面は出ない。トラックが無い間は「イベントを読み込んでいる…」
//   (A2) メニュー: 保存・書き出し・新規・開く・トラックを追加・AI・更新・バージョン情報・終了が無い。ガイド（C++ が対応するまで隠す）・歌詞・原音と比べる・
//        ショートカットだけがある。キーも割り当てない
//   (A3) セッションが来る（session-changed）→ 編集対象を解析して描く。M・S は出さない
//   (A4) 再生ボタン・Space は DAW の再生の制御（transport('toggle')）。Web Audio では鳴らさない
//   (A5) playhead イベント: 再生位置（そのトラックの編集の秒 → タイムライン）・再生中・ループが画面に出る
//   (A6) クリップの位置は動かせない（下半分のドラッグ）。右クリックのメニューは「ガイド」だけ・「ここを下に表示」だけ
//   (A7) ルーラーのクリック → seek、ドラッグ → loop（上下とも）。クリックはループを解除しない。メニューの「ループを解除」→ loop(null)
//   (A8) selection イベント: DAW で選ばれたリージョンのトラックに切り替え、表示範囲をリージョンに寄せる。同じトラックなら表示範囲だけ
//   (A9) リージョンの枠・キャッシュの状態の印と札・エンジンが落ちた箱とつなぎ直し
//   (A10) プロジェクトの操作（保存・書き出し・開く）は通さない
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-ara-spec-'));
const WAV_A = path.join(ROOT, 'vocal-a.wav');
const WAV_B = path.join(ROOT, 'vocal-b.wav');

/** 倍音のある合成の歌（ビブラート付き）。notes: [開始秒, 終わり秒, MIDI]。 */
function writeSong(file, notes, sec = 5, sr = 44100) {
  const n = sr * sec;
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  let ph = 0;
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    const note = notes.find(([a, b]) => t >= a && t < b);
    let env = 0;
    let f = 220;
    if (note) {
      const [a, b, m] = note;
      env = 0.25 * Math.min(1, (t - a) / 0.04, (b - t) / 0.04);
      f = 440 * 2 ** ((m - 69) / 12 + Math.sin(2 * Math.PI * 5.5 * (t - a)) * 0.003);
    }
    ph += 2 * Math.PI * f / sr;
    let x = 0;
    for (let k = 1; k <= 6; k++) x += Math.sin(k * ph) / k;
    buf.writeInt16LE(Math.round(env * x * 32767), 44 + i * 2);
  }
  fs.writeFileSync(file, buf);
}

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  writeSong(WAV_A, [[0.4, 1.4, 64], [1.8, 2.8, 67], [3.2, 4.4, 69]]);
  writeSong(WAV_B, [[0.4, 1.6, 60], [2.0, 3.0, 62], [3.4, 4.4, 65]]);
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', GLISS_TEST_ARA: '1',
    VOCAL_ENGINE_AUTO_LYRICS: '0', VOCAL_ENGINE_WORK_DIR: path.join(ROOT, 'work'),
    VOCAL_ENGINE_PROJECTS: path.join(ROOT, 'projects'), VOCAL_ENGINE_LOG_DIR: path.join(ROOT, 'log'),
    GLISS_BRIDGE: path.join(ROOT, 'bridge.json') };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.GLISS_F0_ESTIMATOR;
  app = await electron.launch({ args: [APP, '--user-data-dir', path.join(ROOT, 'userdata'), '--mute'], env });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => !!window.__app && window.api?.mode === 'ara', null, { timeout: 120000 });
});

test.afterAll(async () => {
  // 試験のセッションは無題のプロジェクト（未保存）なので、閉じるときの「保存しますか」を「保存しない」で答えておく
  // （プラグインでは DAW が閉じるのでこの問いは無い。document.kind は 'ara' で未保存にならない）
  await app?.evaluate(({ dialog }) => { dialog.showMessageBox = async () => ({ response: 1 }); }).catch(() => {});
  await app?.close();
  fs.rmSync(ROOT, { recursive: true, force: true });
});

// ---------------------------------------------------------------- 道具
const emit = (name, data = {}) => win.evaluate(([n, d]) => window.api.__araEmit(n, d), [name, data]);
const calls = () => win.evaluate(() => window.api.__araCalls());
const clearCalls = () => win.evaluate(() => window.api.__araClear());
const transports = async () => (await calls()).filter((c) => c.kind === 'transport').map((c) => [c.op, c.arg]);
const tracks = () => win.evaluate(() => window.__app.tracks());
const current = () => win.evaluate(() => window.__app.S.session?.current || null);
const idle = () => win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 240000 });
const status = () => win.evaluate(() => window.__app.status());
const menuLabels = () => win.evaluate(() => {
  const walk = (items) => items.flatMap((i) => (i.type === 'separator' ? [] : i.submenu ? walk(i.submenu) : [i.label]));
  return window.__app.menubar().model.map((top) => ({ top: top.label, items: walk(top.submenu || []) }));
});

async function sessionChanged() {
  await emit('session-changed', { dir: '' });
}

// ---------------------------------------------------------------- 試験
test('(A1) 起動: プラグインのモード・隠すもの・トラックが無い間の案内', async () => {
  await win.waitForFunction(() => /イベントを読み込んでいる/.test(window.__app.status()), null, { timeout: 120000 });
  const t = await win.evaluate(() => {
    const shown = (s) => { const el = document.querySelector(s); return !!el && getComputedStyle(el).display !== 'none' && el.getBoundingClientRect().width > 0; };
    return {
      mode: document.documentElement.dataset.mode, api: window.api.mode, transport: document.documentElement.dataset.transport,
      logo: shown('.titlebar .logo'), upd: shown('#updNote'), first: shown('#firstRun'), tb: shown('.tb'), play: shown('#bPlay'),
      menubar: shown('#menubar'), chip: document.querySelector('#araChip').textContent, ctrls: !!document.querySelector('#mock.first-run'),
    };
  });
  expect(t).toMatchObject({ mode: 'ara', api: 'ara', transport: 'on', logo: false, upd: false, first: false, tb: true, play: true, menubar: true, ctrls: false });
  expect(await status()).toContain('Gliss を割り当てる');
  expect(await calls()).toEqual([]);                       // 画面が勝手に DAW の再生を触らない
});

test('(A2) メニュー: プロジェクト・書き出し・AI・更新が無い。キーも割り当てない', async () => {
  await win.waitForFunction(() => window.__app.menubar().model.some((m) => m.label === 'ヘルプ'), null, { timeout: 30000 });
  const m = await menuLabels();
  expect(m.map((x) => x.top)).toEqual(['ファイル', '編集', 'ノート', '表示', 'ヘルプ']);
  const by = Object.fromEntries(m.map((x) => [x.top, x.items]));
  expect(by.ファイル).toEqual(['歌詞を読み込む（テキスト）…', '歌詞を読み込む（SVP / MIDI）…']);      // ガイドを開く…は C++ が対応するまで隠す
  expect(by.ヘルプ).toEqual(['ショートカット（キー・ホイール）…']);
  expect(by.表示).toContain('原音と比べる');
  expect(by.編集).toEqual(expect.arrayContaining(['元に戻す', 'やり直す', 'すべて選択', 'テンポを入力', 'RMVPE（既定）', 'Gliss（試作）', 'Praat']));
  const all = m.flatMap((x) => x.items).join('\n');
  for (const gone of ['保存', '書き出し', '新規プロジェクト', '開く…', 'トラックを追加', 'AI とつなぐ', '更新を確認', 'について', '終了', '最近使った', 'モデルと追加']) {
    expect(all, gone).not.toContain(gone);
  }
  const keys = await win.evaluate(() => Object.fromEntries(window.__app.commands()
    .filter((c) => ['new-project', 'open-take', 'save', 'save-as', 'add-track', 'export', 'export-as', 'rename', 'play', 'undo'].includes(c.id))
    .map((c) => [c.id, c.keys])));
  expect(keys).toMatchObject({ 'new-project': [], 'open-take': [], save: [], 'save-as': [], 'add-track': [], export: [], 'export-as': [], rename: [], play: ['Space'], undo: ['Ctrl+Z'] });
});

test('(A3) セッションが来ると、編集対象を解析して描く（M・S・音量・パンは出さない）', async () => {
  // DAW の代わりに、既存のツールでセッションを作る（プラグインではエンジンの ara_* が作る）
  await win.evaluate(async ([a, b]) => {
    const r = await window.api.call('new_project', { take_path: a, author: 'human' });
    if (r.ok === false) throw new Error(r.error);
    const s = await window.api.call('add_track', { path: b, author: 'human' });
    if (s.ok === false) throw new Error(s.error);
  }, [WAV_A, WAV_B]);
  await sessionChanged();
  await win.waitForFunction(() => window.__app.ready() && window.__app.S.tracks.length === 2, null, { timeout: 240000 });
  await idle();
  const ts = await tracks();
  expect(ts.map((t) => t.kind)).toEqual(['vocal', 'vocal']);
  expect(await current()).toBe(ts[0].id);
  const n = await win.evaluate(() => window.__app.notes().length);
  expect(n).toBeGreaterThanOrEqual(2);
  // ミュート／ソロ・音量・パンは隠す（DAW が鳴らす）・ガイドのアイコンは残す
  const vis = await win.evaluate(() => {
    const shown = (el) => !!el && getComputedStyle(el).display !== 'none';
    const h = document.querySelector('#heads .th');
    return { m: shown(h.querySelector('[data-act=m]')), s: shown(h.querySelector('[data-act=s]')), g: shown(h.querySelector('[data-act=guide]')),
      mix: h.querySelectorAll('[data-mix]').length };
  });
  expect(vis).toEqual({ m: false, s: false, g: true, mix: 0 });
  // 初回の「解析中」の覆い・ポップアップは出さない（DAW の操作を止めない）
  const pop = await win.evaluate(() => ({ center: getComputedStyle(document.querySelector('#busyCenter')).display, dim: getComputedStyle(document.querySelector('.busy-scrim'), '::before').display }));
  expect(pop).toEqual({ center: 'none', dim: 'none' });
});

test('(A3b) 解析中（止める処理）は札に出る。終わると「準備完了」が 1.5 秒出て消える', async () => {
  await win.evaluate(async () => {
    const m = await import('./busy.js');
    window.__testBusy = m.laterBusy({ label: 'トラックを準備している', target: 'vocal-a' });
    window.__testBusy.start().update(0.42, 'トラックの解析');
  });
  const chip = win.locator('#araChip .chip.an');
  await expect(chip).toContainText('解析中');
  await expect(chip).toContainText('42%');
  await expect(chip).toContainText('トラックの解析');
  await expect(chip).toContainText('原音を再生中');
  await win.evaluate(() => window.__testBusy.finish());
  await expect(win.locator('#araChip .chip.ok')).toContainText('準備完了');
  await expect(win.locator('#araChip .chip')).toHaveCount(0, { timeout: 5000 });
});

test('(A4) 再生ボタン・Space は DAW の再生の制御へ（Web Audio で鳴らさない）', async () => {
  await clearCalls();
  await win.locator('#bPlay').click();
  await expect.poll(transports).toEqual([['toggle', null]]);
  expect(await win.evaluate(() => window.__app.S.playing)).toBe(false);       // 再生中かどうかは DAW が決める（playhead イベント）
  expect(await win.evaluate(() => window.__app.playState())).toBeNull();
  await win.locator('#mock').focus();
  await win.keyboard.press('Space');
  await expect.poll(transports).toEqual([['toggle', null], ['toggle', null]]);
  expect(await win.evaluate(() => window.__app.playState())).toBeNull();
});

test('(A5) playhead: 再生位置・再生中・ループが画面に出る', async () => {
  const id = await current();
  const off = await win.evaluate(() => window.__app.S.off);
  await emit('playhead', { song_sec: 9.9, playing: true, loop: null, mapped: { [id]: 1.25 } });
  await expect.poll(() => win.evaluate(() => window.__app.S.head)).toBeCloseTo(off + 1.25, 6);   // 鳴っているリージョンの中の編集の秒
  expect(await win.evaluate(() => window.__app.S.playing)).toBe(true);
  expect(await win.evaluate(() => ({ stop: !document.querySelector('#icStop').hasAttribute('hidden'), play: !document.querySelector('#icPlay').hasAttribute('hidden') }))).toEqual({ stop: true, play: false });
  expect(await win.evaluate(() => document.querySelector('#clock').textContent)).toBe('0:01.250');
  // 鳴っていない（そのトラックに対応するリージョンが無い）ときはソングの秒のまま
  await emit('playhead', { song_sec: 2.5, playing: true, loop: null, mapped: { [id]: null } });
  await expect.poll(() => win.evaluate(() => window.__app.S.head)).toBeCloseTo(2.5, 6);
  // ループ（タイムラインの秒のまま）
  await emit('playhead', { song_sec: 1, playing: true, loop: [0.5, 2], mapped: {} });
  await expect.poll(() => win.evaluate(() => window.__app.S.loop)).toEqual([0.5, 2]);
  await emit('playhead', { song_sec: 1.1, playing: false, loop: null, mapped: {} });
  await expect.poll(() => win.evaluate(() => window.__app.S.loop)).toBeNull();
  expect(await win.evaluate(() => window.__app.S.playing)).toBe(false);
  expect(await win.evaluate(() => ({ stop: !document.querySelector('#icStop').hasAttribute('hidden'), play: !document.querySelector('#icPlay').hasAttribute('hidden') }))).toEqual({ stop: false, play: true });
});

test('(A6) クリップの位置は動かせない・右クリックのメニューは DAW が決めないものだけ', async () => {
  const before = await tracks();
  await clearCalls();
  const box = await win.locator('#lanes [data-clip]').first().boundingBox();
  const y = box.y + box.height * 0.8;                 // 下半分（Electron では位置のドラッグ）
  await win.mouse.move(box.x + box.width / 2, y);
  await win.mouse.down();
  await win.mouse.move(box.x + box.width / 2 + 60, y, { steps: 6 });
  await win.mouse.up();
  await idle();
  const after = await tracks();
  expect(after.map((t) => t.offset_sec)).toEqual(before.map((t) => t.offset_sec));
  expect(await win.evaluate(() => window.__app.S.trackOff.size)).toBe(0);
  const cursor = await win.evaluate(() => document.querySelector('#lanes').style.cursor);
  expect(cursor).not.toBe('grabbing');
  // メニュー
  await win.locator('#lanes [data-clip]').first().click({ button: 'right', position: { x: 20, y: 6 }, force: true });
  const clip = await win.evaluate(() => window.__app.menuItems().map((i) => i.label));
  expect(clip).toEqual(['ここを下に表示']);
  await win.keyboard.press('Escape');
  await win.locator('#heads .th').first().click({ button: 'right' });
  const head = await win.evaluate(() => window.__app.menuItems().map((i) => i.label));
  expect(head).toEqual(['このトラックをガイドにする']);
  await win.keyboard.press('Escape');
  // はさみ・ミュートのツールは、トラックビューではクリップを分けない・消さない（クリップは DAW のリージョン）
  for (const tool of ['tool-cut', 'tool-mute']) {
    await win.evaluate((c) => window.__app.runCommand(c), tool);
    const b = await win.locator('#lanes [data-clip]').first().boundingBox();
    await win.mouse.click(b.x + b.width * 0.4, b.y + b.height * 0.3);
    await idle();
    const cls = await win.evaluate(() => document.querySelector('#lanes').getAttribute('class') || '');
    expect(cls).not.toMatch(/cur-(cut|mute)/);
  }
  await win.evaluate(() => window.__app.runCommand('tool-main'));
  const st = await win.evaluate(() => window.__app.S.tracks.map((t) => [(t.cuts || []).length, (t.mutes || []).length]));
  expect(st.every(([c, m]) => c === 0 && m === 0)).toBe(true);
});

test('(A7) ルーラー: クリック → seek、ドラッグ → loop。クリックはループを解除しない', async () => {
  await clearCalls();
  const r = await win.locator('#tvRuler').boundingBox();
  const range = await win.evaluate(() => window.__app.tracksState().range);
  const tAt = (x) => range[0] + (x - r.x) / r.width * (range[1] - range[0]);
  // クリック
  await win.mouse.click(r.x + r.width * 0.3, r.y + 10);
  await expect.poll(transports).toHaveLength(1);
  const [op, arg] = (await transports())[0];
  expect(op).toBe('seek');
  expect(arg.song_sec).toBeCloseTo(tAt(r.x + r.width * 0.3), 1);
  expect(await win.evaluate(() => window.__app.S.head)).toBeCloseTo(arg.song_sec, 6);
  // ドラッグ（上のルーラー）→ loop（ソングの秒）
  await clearCalls();
  await win.mouse.move(r.x + r.width * 0.2, r.y + 10);
  await win.mouse.down();
  await win.mouse.move(r.x + r.width * 0.5, r.y + 10, { steps: 6 });
  await win.mouse.up();
  await expect.poll(transports).toHaveLength(1);
  const [lop, larg] = (await transports())[0];
  expect(lop).toBe('loop');
  expect(larg.track_id).toBeUndefined();
  expect(larg.a).toBeLessThan(larg.b);
  expect(await win.evaluate(() => window.__app.S.loop)).toEqual([larg.a, larg.b]);
  // クリックしてもループは残る（解除はメニュー）。DAW のループは DAW のもの
  await clearCalls();
  await win.mouse.click(r.x + r.width * 0.8, r.y + 10);
  await expect.poll(transports).toHaveLength(1);
  expect((await transports())[0][0]).toBe('seek');
  expect(await win.evaluate(() => window.__app.S.loop)).toEqual([larg.a, larg.b]);
  // 下のルーラー: seek は編集の秒（track_id つき）、loop も
  await clearCalls();
  const id = await current();
  const sc = await win.locator('#roll rect[data-scale]').first().boundingBox();
  const off = await win.evaluate(() => window.__app.S.off);
  await win.mouse.click(sc.x + sc.width * 0.6, sc.y + sc.height / 2);
  await expect.poll(transports).toHaveLength(1);
  const [eop, earg] = (await transports())[0];
  expect(eop).toBe('seek');
  expect(earg.track_id).toBe(id);
  expect(await win.evaluate((o) => window.__app.S.head - o, off)).toBeCloseTo(earg.sec, 6);
  await clearCalls();
  await win.mouse.move(sc.x + sc.width * 0.3, sc.y + sc.height / 2);
  await win.mouse.down();
  await win.mouse.move(sc.x + sc.width * 0.6, sc.y + sc.height / 2, { steps: 6 });
  await win.mouse.up();
  await expect.poll(transports).toHaveLength(1);
  const [eop2, earg2] = (await transports())[0];
  expect(eop2).toBe('loop');
  expect(earg2.track_id).toBe(id);
  expect(earg2.a).toBeLessThan(earg2.b);
  expect(await win.evaluate((o) => window.__app.S.loop.map((v) => +(v - o).toFixed(6)), off)).toEqual([+earg2.a.toFixed(6), +earg2.b.toFixed(6)]);
  // ルーラーの右クリック →「ループを解除」→ loop(null)
  await clearCalls();
  await win.locator('#tvRuler').click({ button: 'right', position: { x: 40, y: 8 } });
  await win.locator('#menu button', { hasText: 'ループを解除' }).click();
  await expect.poll(transports).toEqual([['loop', null]]);
  expect(await win.evaluate(() => window.__app.S.loop)).toBeNull();
});

test('(A8) selection: DAW で選んだリージョンのトラックに切り替え、表示範囲を寄せる', async () => {
  const ts = await tracks();
  const [first, second] = ts;
  const offB = second.offset_sec || 0;
  await emit('selection', { track_id: second.id, ara_id: 'mod-b',
    region: { id: 'r-b', song_start: offB + 1, song_end: offB + 3, mod_start: 1, mod_end: 3 } });
  await expect.poll(current, { timeout: 120000 }).toBe(second.id);
  await idle();
  const v = await win.evaluate(() => window.__app.view());
  expect(v.t0).toBeCloseTo(1, 1);
  expect(v.span).toBeCloseTo(2, 1);
  expect((await win.evaluate(() => window.__app.S.take?.path || '')).toLowerCase()).toContain('vocal-b');
  // 同じトラックなら表示範囲だけ寄せる（切り替えない）
  await emit('selection', { track_id: second.id, ara_id: 'mod-b',
    region: { id: 'r-b', song_start: offB + 2, song_end: offB + 4, mod_start: 2, mod_end: 4 } });
  await expect.poll(() => win.evaluate(() => window.__app.view().t0)).toBeCloseTo(2, 1);
  expect((await win.evaluate(() => window.__app.view())).span).toBeCloseTo(2, 1);
  expect(await current()).toBe(second.id);
  // 全体を選んだだけ（同じトラック）は、利用者のズームを動かさない
  await emit('selection', { track_id: second.id, ara_id: 'mod-b',
    region: { id: 'r-b', song_start: offB, song_end: offB + 5, mod_start: 0, mod_end: 5 } });
  await win.waitForTimeout(400);
  expect((await win.evaluate(() => window.__app.view())).t0).toBeCloseTo(2, 1);
  // ドラッグ中は最後の 1 件だけ覚えて、終わってから当てる
  await win.evaluate(() => { window.__app.S.drag = { type: 'test' }; });
  await emit('selection', { track_id: first.id, ara_id: 'mod-a', region: { id: 'r-a', song_start: 0, song_end: 2, mod_start: 0, mod_end: 2 } });
  await emit('selection', { track_id: second.id, ara_id: 'mod-b', region: { id: 'r-b', song_start: offB + 3, song_end: offB + 4, mod_start: 3, mod_end: 4 } });
  await win.waitForTimeout(500);
  expect(await current()).toBe(second.id);              // まだ切り替えない
  await win.evaluate(() => { window.__app.S.drag = null; });
  await expect.poll(() => win.evaluate(() => window.__app.view().t0), { timeout: 60000 }).toBeCloseTo(3, 1);
  expect(await current()).toBe(second.id);              // 最後の 1 件（second）だけ当たる
  // 戻す
  await emit('selection', { track_id: first.id, ara_id: 'mod-a', region: { id: 'r-a', song_start: 0, song_end: 2, mod_start: 0, mod_end: 2 } });
  await expect.poll(current, { timeout: 120000 }).toBe(first.id);
  await idle();
});

test('(A9) リージョンの枠・キャッシュの状態の印と札・エンジンが落ちた箱', async () => {
  const [a, b] = await tracks();
  const rA = [{ id: 'r1', song_start: 0.5, song_end: 2.5, mod_start: 0.5, mod_end: 2.5 },
    { id: 'r2', song_start: 6, song_end: 8, mod_start: 0.5, mod_end: 2.5 }];
  await win.evaluate(([x, y, rs]) => window.api.__araSetHost({ tracks: [
    { track_id: x, ara_id: 'mod-a', regions: rs, cache: { state: 'ready' } },
    { track_id: y, ara_id: 'mod-b', regions: [], cache: { state: 'ready' } }] }), [a.id, b.id, rA]);
  await sessionChanged();
  // 枠: 複製したリージョンも別の枠（どれも同じトラックを選ぶ）。全体の薄い波形の上に、枠の中だけ明るく
  await expect.poll(() => win.evaluate((id) => document.querySelectorAll(`#lanes [data-clip="${id}"][data-region]`).length, a.id)).toBe(2);
  const st = await win.evaluate(() => window.__app.tracksState());
  expect(st.range[1]).toBeGreaterThan(8);                          // 複製したリージョンまで目盛りが伸びる
  // 2 つ目の枠（代表の位置より後ろ）をクリック → 同じトラック。下の表示は編集の秒（0.5〜2.5 付近）
  const second = await win.locator(`#lanes [data-clip="${a.id}"][data-region="r2"]`).boundingBox();
  await clearCalls();
  await win.mouse.click(second.x + second.width / 2, second.y + 6);
  await idle();
  expect(await current()).toBe(a.id);
  expect(await transports()).toEqual([]);                          // トラックを選ぶだけでは DAW の再生位置は動かさない
  // 枠の外（薄い波形の上）のクリックは再生位置だけ（seek）
  await clearCalls();
  const outer = await win.locator(`#lanes [data-clip="${a.id}"][data-region="r1"]`).boundingBox();
  await win.mouse.click(outer.x + outer.width + 40, outer.y + 6);
  await expect.poll(transports).toHaveLength(1);
  expect((await transports())[0][0]).toBe('seek');
  // キャッシュの状態: 読み込み中（見出しの印と札）→ 準備完了の札 → 消える
  await emit('cache', { track_id: a.id, state: 'reading', progress: 0.4 });
  await expect(win.locator(`#heads .pp[data-pp="${a.id}"]`)).toHaveAttribute('data-state', 'preparing');
  await expect(win.locator('#araChip .chip.an')).toContainText('ソースを読み込んでいる');
  await expect(win.locator('#araChip .chip.an')).toContainText('40%');
  await emit('cache', { track_id: a.id, state: 'ready' });
  await expect(win.locator('#araChip .chip.ok')).toContainText('準備完了');
  await expect(win.locator('#araChip .chip')).toHaveCount(0, { timeout: 5000 });          // 1.5 秒で消える
  await emit('cache', { track_id: a.id, state: 'mismatch' });
  await expect(win.locator('#araChip .chip.off')).toContainText('DAW の音が変わった');
  await expect(win.locator(`#heads .pp[data-pp="${a.id}"]`)).toHaveAttribute('data-state', 'failed');
  await emit('cache', { track_id: a.id, state: 'failed', error: 'モデルが読めない' });
  await expect(win.locator('#araChip .chip.err')).toContainText('解析できなかった');
  await expect(win.locator('#araBox .bx')).toContainText('モデルが読めない');
  await emit('cache', { track_id: a.id, state: 'ready' });
  await expect(win.locator('#araBox .bx')).toHaveCount(0);
  // エンジンが落ちた: 札（切れた線）と中央の箱。つなぎ直す → restartEngine
  await emit('cache', { track_id: a.id, state: 'ready' });
  await emit('engine', { state: 'failed', error: 'プロセスが終わった' });
  await expect(win.locator('#araChip .chip.off')).toContainText('エンジンにつながらない');
  await expect(win.locator('#araBox .bx')).toContainText('編集は DAW のソングに残っている');
  await clearCalls();
  await win.locator('#araBox button', { hasText: 'つなぎ直す' }).click();
  await expect.poll(async () => (await calls()).filter((c) => c.kind === 'restartEngine').length).toBe(1);
  await emit('engine', { state: 'ready' });
  await expect(win.locator('#araBox .bx')).toHaveCount(0);
  // 状態を取りこぼしても hostState で引き直せる（画面が見えたとき）
  await win.evaluate(([x]) => window.api.__araSetHost({ tracks: [{ track_id: x, regions: [], cache: { state: 'syncing' } }] }), [a.id]);
  await win.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(win.locator('#araChip .chip.an')).toContainText('編集を反映している');
});

test('(A10) プロジェクトの操作（保存・書き出し・開く・ガイドを開く）は通さない', async () => {
  for (const cmd of ['save', 'save-as', 'export', 'export-as', 'new-project', 'open-take', 'add-track', 'open-guide']) {
    const r = await win.evaluate((c) => window.__app.onMenu({ cmd: c }), cmd);
    expect(r, cmd).toBe(false);
  }
  expect(await status()).toContain('DAW のプラグインでは使えない');
  expect(await win.evaluate(() => window.__app.runCommand('save'))).toBeUndefined();
  // 原音と比べる: ドキュメント全体のフラグ（native の setCompare）
  await clearCalls();
  await win.evaluate(() => window.__app.runCommand('ara-compare'));
  await expect.poll(async () => (await calls()).filter((c) => c.kind === 'setCompare')).toEqual([{ kind: 'setCompare', on: true }]);
  await win.evaluate(() => window.__app.runCommand('ara-compare'));
  await expect.poll(async () => (await calls()).filter((c) => c.kind === 'setCompare').length).toBe(2);
  expect((await calls()).filter((c) => c.kind === 'setCompare')[1].on).toBe(false);
});

test('エラーが出ていない', () => {
  expect(errors).toEqual([]);
});
