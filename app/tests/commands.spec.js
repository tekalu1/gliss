// 右クリックのメニュー（issue #17）とキーボードショートカットの設定（issue #22）。
//
//   (C1) ノート: ガイドに合わせる…G／半音に合わせる Q／ここで分ける Alt+X／なだらかさ… T／オリジナルに戻す／無音にする Del／AI に頼む
//   (C2) 複数選択: 「ここで分ける」の代わりに 結合 Ctrl+J
//   (C3) 境目: 結合／切り離す（Alt+ドラッグ）／なだらかさ…、切り離すを押すと切れて Ctrl+Z で戻る
//   (C4) 空白・ルーラー・歌詞レーン・音素のメニュー（スナップ・テンポは #18 で有効。grid-fade.spec.js）
//   (C5) トラック見出し（名前を変える F2）・クリップ
//   (C6) 新しいキー: Q（半音）・Del（無音）・Alt+X（再生位置で分ける）・Ctrl+J（結合）・Ctrl+A・F2、どれも 1 回で戻せる
//   (C7) メニューバー（タイトルバー。中身は main の Electron の Menu）の名前・キーもコマンドの表から
//   (K1) 設定画面: Ctrl+, で開く・検索・ダブルクリックしてキー・重なりの「置き換える／やめる」・既定に戻す／外す・すべて既定に戻す
//   (K2) 変えたキーが効き、メニュー・メニューバー・ツールチップの表記も変わる。userData に保存、取り消しの履歴には入らない
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
const PROJECT = path.join(REPO, 'projects', '_test-commands');
const USERDATA = `${PROJECT}-userdata`;
const DOCS = path.join(APP, 'screenshots');

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
    args: [APP, '--take', TAKE, '--guide', GUIDE, '--project-dir', PROJECT,
      '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await showRange(2.2, 3.5);
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 120000 });
}
async function showRange(a, b) {
  await win.evaluate(([t0, t1]) => {
    window.__app.S.view = { t0, span: t1 - t0 };
    window.__app.render();
  }, [a, b]);
}
const blob = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();
const items = () => win.evaluate(() => window.__app.menuItems());
const notes = () => win.evaluate(() => window.__app.notes());
const hist = () => win.evaluate(() => window.__app.hist());
// タイトルバーのメニューバー（main の Menu を写したものを titlebar.js が描いている）
const menuBar = () => win.evaluate(() => window.__app.menubar().model.map((m) => ({
  label: m.label,
  items: m.submenu.filter((x) => x.type !== 'separator').map((x) => ({
    label: x.label, accel: x.accelerator || null, enabled: x.enabled, checked: x.checked,
  })),
})));
async function escape() {
  await win.keyboard.press('Escape');
  await expect(win.locator('#menu')).toBeHidden();
}
async function select(ids) {
  await win.evaluate((s) => { window.__app.S.sel = s; window.__app.render(); }, ids);
}
/** 接して並ぶ（結合できる）音程ノートの組（画面に見えるもの）。 */
async function touchingPair() {
  return win.evaluate(() => {
    const P = window.__app.S.pitched;
    const v = window.__app.S.view;
    for (let i = 0; i + 1 < P.length; i++) {
      const a = P[i]; const b = P[i + 1];
      if (Math.abs(b.start_sec - a.end_sec) <= 0.006 && a.connected_next
        && a.edited_start_sec > v.t0 && b.edited_end_sec < v.t0 + v.span) return [a.id, b.id];
    }
    return null;
  });
}

test('(C1) ノートの右クリック: 7 項目・キーを右に', async () => {
  await select([]);
  await blob('n014').click({ button: 'right' });
  await expect(win.locator('#menu')).toBeVisible();
  const it = await items();
  expect(it.map((x) => [x.label, x.key])).toEqual([
    ['ガイドに合わせる…', 'G'], ['半音に合わせる', 'Q'], ['ここで分ける', 'Alt+X'],
    ['なだらかさ…', 'T'], ['オリジナルに戻す', ''], ['無音にする', 'Del'], ['AI に頼む', ''],
  ]);
  expect(it.length).toBeGreaterThanOrEqual(3);
  expect(it.length).toBeLessThanOrEqual(7);
  // 未選択のノートを右クリックすると、そのノートを選んでから出す
  expect(await win.evaluate(() => window.__app.S.sel)).toEqual(['n014']);
  // キーの表記は右にそろえて出る（DOM）
  await expect(win.locator('#menu [data-cmd="semitone"] .k')).toHaveText('Q');
  await win.screenshot({ path: path.join(DOCS, 'screenshot-menu-note.png') });
  await escape();
  expect(await win.evaluate(() => window.__app.S.sel)).toEqual(['n014']);   // Esc はメニューだけ閉じる
});

test('(C2) 複数選択: 結合 Ctrl+J（隣り合って接していれば押せる）', async () => {
  const pair = await touchingPair();
  expect(pair).not.toBeNull();
  await select(pair);
  await blob(pair[0]).click({ button: 'right' });
  const it = await items();
  expect(it.map((x) => x.label)).toEqual(['ガイドに合わせる…', '半音に合わせる', '結合', 'なだらかさ…', 'オリジナルに戻す', '無音にする', 'AI に頼む']);
  expect(it.find((x) => x.label === '結合')).toMatchObject({ key: 'Ctrl+J', disabled: false });
  await escape();
  // 離れたノートの組は結合できない（グレー）
  await select(['n005', 'n014']);
  await blob('n014').click({ button: 'right' });
  expect((await items()).find((x) => x.label === '結合').disabled).toBe(true);
  await escape();
  await select([]);
});

test('(C3) 境目の右クリック: 結合／切り離す／なだらかさ…、切り離すは Ctrl+Z で戻る', async () => {
  const [a, b] = await touchingPair();
  const edge = win.locator(`#roll rect[data-note="${a}"][data-edge="end"]`);
  await edge.click({ button: 'right' });
  const it = await items();
  expect(it.map((x) => [x.label, x.key])).toEqual([['結合', 'Ctrl+J'], ['切り離す', 'Alt+ドラッグ'], ['なだらかさ…', 'T']]);
  await win.locator('#menu [data-item="detach"]').click();
  await settle();
  const conn = async () => (await win.evaluate(() => window.__app.connections())).find((c) => c.id === a).next;
  expect(await conn()).toBe(false);
  expect(await win.evaluate(() => window.__app.undoTitle())).toBe('元に戻す: 切り離し（Ctrl+Z）');
  // 切り離した境目: 「つなぐ」に変わる（接していればそのまま接続に戻す）
  await edge.click({ button: 'right' });
  expect((await items()).map((x) => [x.label, x.disabled])).toEqual([['結合', false], ['つなぐ', false], ['なだらかさ…', true]]);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-menu-boundary.png') });
  await escape();
  expect(b).toBeTruthy();
  await win.keyboard.press('Control+z');
  await settle();
  expect(await conn()).toBe(true);
});

test('(C3b) 隙間のある境目の右クリック: つなぐ = 前のノートを伸ばして接続（Ctrl+Z で戻る）', async () => {
  await showRange(0, 4);
  // 準備: 接して並ぶ組の前のノートを切り離して 60 ms 縮め、隙間を作る（Alt+ドラッグと同じ計画）
  const [pa] = await touchingPair();
  await win.evaluate(async (id) => {
    const r = await window.api.call('plan_edit', { op: 'edge', note_id: id, side: 'end', detach: true });
    await window.api.call('apply_plan', { plan_id: r.plan_id, x: -0.06, author: 'human' });
    await window.__app.refresh();
  }, pa);
  const g = await win.evaluate(() => {
    const P = window.__app.S.pitched;
    const sh = new Map(window.__app.shapes().map((x) => [x.id, x]));
    const r = document.querySelector('#roll').getBoundingClientRect();
    const v = window.__app.S.view;
    const X = (t) => 44 + (t - v.t0) / v.span * (r.width - 44);
    for (let i = 0; i + 1 < P.length; i++) {
      const a = sh.get(P[i].id); const b = sh.get(P[i + 1].id);
      const gap = b.s - a.e;
      if (gap > 0.03 && gap < 0.25 && !P[i].connected_next && a.s > v.t0 && b.e < v.t0 + v.span) {
        return { a: P[i].id, b: P[i + 1].id, x: r.left + (X(a.e) + X(b.s)) / 2, band: (a.band + b.band) / 2 };
      }
    }
    return null;
  });
  expect(g).not.toBeNull();
  const y = await win.evaluate((m) => {
    const S = window.__app.S; const r = document.querySelector('#roll').getBoundingClientRect();
    const rollH = r.height - 20 - 46;
    return r.top + 20 + (S.pv.top - m) * (rollH / S.pv.span);
  }, g.band);
  await win.mouse.click(g.x, y, { button: 'right' });
  const it = await items();
  expect(it.map((x) => [x.label, x.disabled])).toEqual([['結合', false], ['つなぐ', false], ['なだらかさ…', true]]);
  const n0 = (await notes()).length;
  await win.locator('#menu [data-item="connect"]').click();
  await settle();
  const c = (await win.evaluate(() => window.__app.connections())).find((x) => x.id === g.a);
  expect(c.next).toBe(true);
  const sh = new Map((await win.evaluate(() => window.__app.shapes())).map((x) => [x.id, x]));
  expect(Math.abs(sh.get(g.b).s - sh.get(g.a).e)).toBeLessThan(0.002);    // 前のノートが次の頭まで伸びた
  expect(await win.evaluate(() => window.__app.undoTitle())).toBe('元に戻す: つなぐ（Ctrl+Z）');
  expect((await notes()).length).toBe(n0);
  await win.keyboard.press('Control+z');
  await settle();
  expect((await win.evaluate(() => window.__app.connections())).find((x) => x.id === g.a).next).toBe(false);
  await win.keyboard.press('Control+z');                                   // 準備の分
  await settle();
  expect(await win.evaluate(() => window.__app.S.vd.edits.length)).toBe(0);
  await showRange(2.2, 3.5);
});

test('(C4) 空白・ルーラー・歌詞レーン・音素', async () => {
  const r = await win.locator('#roll').boundingBox();
  const top = await win.evaluate(() => window.__app.S.pv.top);
  // 空白（ピアノロールのいちばん上の行の右端。ノートの無い所）
  await win.evaluate(() => { window.__app.S.pv = { top: window.__app.S.pv.top + 8, span: window.__app.S.pv.span }; window.__app.render(); });
  await win.mouse.click(r.x + r.width - 30, r.y + 40, { button: 'right' });
  let it = await items();
  expect(it.map((x) => [x.label, x.key, x.disabled])).toEqual([
    ['すべて選択', 'Ctrl+A', false], ['時間スナップ', 'N', false], ['音程スナップ（半音）', 'Shift+N', false], ['全体を表示', '', false],
  ]);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-menu-empty.png') });
  await escape();
  await win.evaluate((t) => { window.__app.S.pv = { top: t, span: window.__app.S.pv.span }; window.__app.render(); }, top);
  // ルーラー（下）
  await win.mouse.click(r.x + r.width / 2, r.y + 8, { button: 'right' });
  it = await items();
  expect(it.map((x) => [x.label, x.disabled, x.checked])).toEqual([
    ['ループを解除', true, false], ['テンポと拍子…', false, false], ['表示: 小節・拍', true, false], ['表示: 分:秒', false, true],
  ]);
  await escape();
  // ルーラー（上のトラックビュー）も同じ
  const tr = await win.locator('#tvRuler').boundingBox();
  await win.mouse.click(tr.x + tr.width / 2, tr.y + tr.height / 2, { button: 'right' });
  expect((await items()).map((x) => x.label)).toEqual(['ループを解除', 'テンポと拍子…', '表示: 小節・拍', '表示: 分:秒']);
  await escape();
  // 歌詞レーン（歌詞なし）: 入力… / 消す（グレー） / 聞き取る（issue #54。使えるかは faster-whisper による） / 読み込む…
  const n = (await win.evaluate(() => window.__app.shapes())).find((x) => x.id === 'n014');
  const x = await win.evaluate((t) => { const s = window.__app.S; const W = document.querySelector('#roll').getBoundingClientRect().width; return 44 + (t - s.view.t0) / s.view.span * (W - 44); }, (n.s + n.e) / 2);
  await win.mouse.click(r.x + x, r.y + r.height - 30, { button: 'right' });
  it = await items();
  expect(it.map((x) => [x.label, x.key, x.cmd === 'transcribe' ? null : x.disabled])).toEqual([
    ['歌詞を入力…', 'ダブルクリック', false], ['この区間の歌詞を消す', '', true], ['聞き取る', '', null],
    ['歌詞を読み込む…', '', false],
  ]);
  await escape();
});

test('(C5) トラック見出し（名前を変える F2）とクリップ', async () => {
  const ts = await win.evaluate(() => window.__app.tracks());
  expect(ts.length).toBeGreaterThanOrEqual(2);
  const cur = ts.find((t) => t.current);
  await win.locator(`#heads .th[data-id="${cur.id}"] .nm`).click({ button: 'right' });
  let it = await items();
  expect(it.map((x) => [x.label, x.key])).toEqual([
    ['名前を変える', 'F2'], ['ガイドを選ぶ…', ''], [cur.guide ? '共通のガイドから外す' : 'このトラックを共通のガイドにする', ''],
    ['元の位置に戻す', ''],
    ['伴奏として扱う', ''], ['トラックを外す', ''],
  ]);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-menu-track.png') });
  await escape();
  const c = await win.locator(`#lanes [data-clip="${cur.id}"]`).boundingBox();
  await win.mouse.click(c.x + 20, c.y + 4, { button: 'right' });
  it = await items();
  expect(it.map((x) => x.label)).toEqual(['ここを下に表示', '元の位置に戻す', 'トラックを外す']);
  await escape();
  // F2: 編集中のトラックの名前を入力欄に（Esc でやめる）
  await win.locator('#roll').click({ position: { x: 5, y: 5 } });
  await win.keyboard.press('F2');
  await expect(win.locator('#heads input.rn')).toBeVisible();
  await win.keyboard.press('Escape');
  await expect(win.locator('#heads input.rn')).toHaveCount(0);
});

test('(C6) Q・Del・Alt+X・Ctrl+J・Ctrl+A、どれも 1 回で戻せる', async () => {
  await showRange(2.2, 3.5);
  // Q: 帯の高さを半音に
  await select(['n014']);
  await win.keyboard.press('q');
  await settle();
  const band = async (id) => (await win.evaluate(() => window.__app.bands())).find((b) => b.id === id).band;
  const b1 = await band('n014');
  expect(Math.abs(b1 - Math.round(b1))).toBeLessThan(0.01);
  expect(await win.evaluate(() => window.__app.undoTitle())).toBe('元に戻す: 半音に合わせる（Ctrl+Z）');
  await win.keyboard.press('Control+z');
  await settle();
  expect(Math.abs((await band('n014')) - Math.round(await band('n014')))).toBeGreaterThan(0.01);
  // Del: 無音にする（帯は薄く）
  await select(['n014']);
  await win.keyboard.press('Delete');
  await settle();
  expect((await notes()).find((n) => n.id === 'n014').muted).toBe(true);
  await expect(win.locator('#roll g.nb.muted')).toHaveCount(1);
  expect(await win.evaluate(() => window.__app.undoTitle())).toBe('元に戻す: 無音にする（Ctrl+Z）');
  await win.screenshot({ path: path.join(DOCS, 'screenshot-muted.png') });
  await win.keyboard.press('Control+z');
  await settle();
  expect((await notes()).find((n) => n.id === 'n014').muted).toBe(false);
  // Alt+X: 再生位置で分ける（選んだノートの上）
  const n0 = (await notes()).length;
  const s = (await win.evaluate(() => window.__app.shapes())).find((x) => x.id === 'n014');
  await win.evaluate((t) => { window.__app.S.head = window.__app.S.off + t; window.__app.render(); }, (s.s + s.e) / 2);
  await select(['n014']);
  await win.keyboard.press('Alt+x');
  await settle();
  const after = await notes();
  expect(after.length).toBe(n0 + 1);
  const right = after.find((n) => n.id.startsWith('n014@'));
  expect(right).toBeTruthy();
  // Ctrl+J: 分けた 2 つを結合
  await select(['n014', right.id]);
  await win.keyboard.press('Control+j');
  await settle();
  expect((await notes()).length).toBe(n0);
  expect(await win.evaluate(() => window.__app.undoTitle())).toBe('元に戻す: 結合（Ctrl+Z）');
  await win.keyboard.press('Control+z');
  await settle();
  await win.keyboard.press('Control+z');
  await settle();
  expect((await notes()).length).toBe(n0);
  expect((await hist())?.can_undo ?? !!(await hist())?.undo).toBeFalsy();
  // 使えないとき（選択なしの Q）は理由をステータス行に出し、メニューバーもグレー
  await select([]);
  await win.keyboard.press('q');
  expect(await win.evaluate(() => window.__app.status())).toBe('半音に合わせる: 今は使えない（ノートを選んでから）');
  // N（時間スナップ）は #18 で使えるようになった: 押すとオン、もう一度でオフ
  await win.keyboard.press('n');
  expect(await win.evaluate(() => window.__app.status())).toContain('時間スナップ: オン');
  await win.keyboard.press('n');
  expect(await win.evaluate(() => window.__app.status())).toContain('時間スナップ: オフ');
  await expect.poll(async () => (await menuBar()).find((m) => m.label === 'ノート').items
    .find((x) => x.label === '半音に合わせる').enabled).toBe(false);
  await select(['n014']);
  await expect.poll(async () => (await menuBar()).find((m) => m.label === 'ノート').items
    .find((x) => x.label === '半音に合わせる').enabled).toBe(true);
  // Ctrl+A: すべて選択
  await win.keyboard.press('Control+a');
  expect((await win.evaluate(() => window.__app.S.sel)).length).toBe((await win.evaluate(() => window.__app.S.blocks.length)));   // 音程ノートも子音・息も
  await win.keyboard.press('Escape');
});

test('(C7) メニューバーの名前・キーもコマンドの表から（ノート・表示を足した）', async () => {
  const mb = await menuBar();
  expect(mb.map((m) => m.label)).toEqual(['ファイル', '編集', 'ノート', '表示', 'ヘルプ']);
  expect(mb.find((m) => m.label === 'ヘルプ').items.map((x) => x.label)).toEqual(['AI とつなぐ…', 'モデルと追加の機能…', '更新を確認…', 'Gliss について']);   // issue #29
  const note = mb.find((m) => m.label === 'ノート').items;
  expect(note.map((x) => [x.label, x.accel])).toEqual([
    ['ガイドに合わせる…', 'G'], ['半音に合わせる', 'Q'], ['ここで分ける', 'Alt+X'], ['結合', 'CmdOrCtrl+J'],
    ['なだらかさ…', 'T'], ['フェードを消す', null], ['オリジナルに戻す', null], ['無音にする', 'Delete'],
    ['無音を戻す', null], ['AI に頼む', null],
  ]);
  const edit = mb.find((m) => m.label === '編集').items;
  expect(edit.map((x) => x.label)).toContain('ショートカット（キー・ホイール）…');
  expect(edit.find((x) => x.label === 'ショートカット（キー・ホイール）…').accel).toBe('CmdOrCtrl+,');
  const view = mb.find((m) => m.label === '表示').items;
  expect(view.find((x) => x.label === '時間スナップ')).toMatchObject({ accel: 'N', enabled: true, checked: false });
  expect(view.find((x) => x.label === '音程スナップ（半音）')).toMatchObject({ accel: 'Shift+N', enabled: true, checked: false });
  expect(edit.map((x) => x.label)).toContain('テンポを入力');
  expect(view.find((x) => x.label === 'ガイドを重ねて表示').checked).toBe(true);
  const file = mb.find((m) => m.label === 'ファイル').items;
  expect(file.find((x) => x.label === '書き出し').accel).toBe('CmdOrCtrl+E');
});

test('(K1) 設定画面: 検索・ダブルクリックしてキー・重なりの置き換え・既定に戻す', async () => {
  const h0 = await hist();
  await win.locator('#roll').click({ position: { x: 5, y: 5 } });
  await win.keyboard.press('Control+,');
  await expect(win.locator('#keys')).toBeVisible();
  await expect(win.locator('#keys .ks')).toBeFocused();
  // グループごとの一覧
  const groups = await win.locator('#keys .kg').allTextContents();
  expect(groups).toEqual(['再生・ツール', '編集', 'ピッチ検出の方式', 'ノート', '歌詞', '表示', 'トラック', 'ファイル', 'ヘルプ', 'ホイール']);
  // 検索（名前・キー）
  await win.locator('#keys .ks').fill('結合');
  await expect(win.locator('#keys .kr')).toHaveCount(1);
  await win.locator('#keys .ks').fill('Ctrl+J');
  await expect(win.locator('#keys .kr')).toHaveCount(1);
  await win.locator('#keys .ks').fill('');
  // 結合を Q に: 重なる（半音に合わせる）→ その場で「置き換える／やめる」
  await win.locator('#keys .kr[data-id="merge"]').dblclick();
  await expect(win.locator('#keys .kr[data-id="merge"] .k')).toHaveText('キーを押してください（Esc でやめる）');
  await win.keyboard.press('q');
  await expect(win.locator('#keys .kc')).toContainText('Q は「半音に合わせる」に割り当て済み');
  await win.screenshot({ path: path.join(DOCS, 'screenshot-keys.png') });
  await win.locator('#keys .kc button[data-b="cancel"]').click();        // やめる: 変わらない
  await expect(win.locator('#keys .kr[data-id="merge"] .k')).toHaveText('Ctrl+J');
  await win.locator('#keys .kr[data-id="merge"]').dblclick();
  await win.keyboard.press('q');
  await win.locator('#keys .kc button[data-b="rep"]').click();           // 置き換える
  await expect(win.locator('#keys .kr[data-id="merge"] .k')).toHaveText('Q');
  await expect(win.locator('#keys .kr[data-id="merge"] .st')).toHaveText('既定: Ctrl+J');
  await expect(win.locator('#keys .kr[data-id="semitone"] .k')).toHaveText('—');
  await expect(win.locator('#keys .kn')).toHaveText('2 件を変更');
  // 割り当て待ちの Esc はやめるだけ（画面は閉じない）
  await win.locator('#keys .kr[data-id="tool-draw"]').dblclick();
  await win.keyboard.press('Escape');
  await expect(win.locator('#keys')).toBeVisible();
  await expect(win.locator('#keys .kr[data-id="tool-draw"] .k')).toHaveText('2');
  // 鉛筆を D に（重ならない）
  await win.locator('#keys .kr[data-id="tool-draw"]').dblclick();
  await win.keyboard.press('d');
  await expect(win.locator('#keys .kr[data-id="tool-draw"] .k')).toHaveText('D');
  // 開いている間はキーがコマンドに行かない（Q を押しても何も起きない）
  // 閉じる（Esc）
  await win.keyboard.press('Escape');
  await expect(win.locator('#keys')).toBeHidden();
  // 設定は取り消しの履歴に入らない
  expect(await hist()).toEqual(h0);
  // userData に保存（既定と違うものだけ）
  const st = JSON.parse(fs.readFileSync(path.join(USERDATA, 'state.json'), 'utf8'));
  expect(st.keys).toEqual({ merge: ['Q'], semitone: [], 'tool-draw': ['D'] });
});

test('(K2) 変えたキーが効き、メニュー・メニューバー・ツールチップの表記も変わる', async () => {
  await win.locator('#roll').click({ position: { x: 5, y: 5 } });
  await win.keyboard.press('d');
  expect(await win.evaluate(() => window.__app.tool())).toBe('draw');
  await win.keyboard.press('2');                                         // もう鉛筆ではない
  expect(await win.evaluate(() => window.__app.tool())).toBe('draw');
  await win.keyboard.press('1');
  expect(await win.evaluate(() => window.__app.tool())).toBe('main');
  expect(await win.locator('#bToolDraw').getAttribute('title')).toBe('鉛筆（D）: ピッチを描く');
  const pair = await touchingPair();
  await select(pair);
  await blob(pair[0]).click({ button: 'right' });
  const it = await items();
  expect(it.find((x) => x.label === '結合').key).toBe('Q');
  expect(it.find((x) => x.label === '半音に合わせる').key).toBe('');
  await escape();
  await expect.poll(async () => (await menuBar()).find((m) => m.label === 'ノート').items
    .find((x) => x.label === '結合').accel).toBe('Q');
  // Q で結合、Ctrl+J は何もしない
  const n0 = (await notes()).length;
  await win.keyboard.press('Control+j');
  await settle();
  expect((await notes()).length).toBe(n0);
  await win.keyboard.press('q');
  await settle();
  expect((await notes()).length).toBe(n0 - 1);
  await win.keyboard.press('Control+z');
  await settle();
  expect((await notes()).length).toBe(n0);
  // 行ごとの既定に戻す・外す・すべて既定に戻す
  await win.keyboard.press('Control+,');
  await win.locator('#keys .kr[data-id="merge"]').hover();
  await win.locator('#keys .kr[data-id="merge"] button[data-b="def"]').click();
  await expect(win.locator('#keys .kr[data-id="merge"] .k')).toHaveText('Ctrl+J');
  await win.locator('#keys .kr[data-id="play"]').hover();
  await win.locator('#keys .kr[data-id="play"] button[data-b="rm"]').click();
  await expect(win.locator('#keys .kr[data-id="play"] .k')).toHaveText('—');
  await win.locator('#keys .kf button[data-b="all"]').click();
  await expect(win.locator('#keys .kn')).toHaveText('');
  await expect(win.locator('#keys .kr[data-id="semitone"] .k')).toHaveText('Q');
  await win.locator('#keys .kh button[data-b="close"]').click();
  await expect(win.locator('#keys')).toBeHidden();
  const st = JSON.parse(fs.readFileSync(path.join(USERDATA, 'state.json'), 'utf8'));
  expect(st.keys).toEqual({});
  expect(await win.locator('#bToolDraw').getAttribute('title')).toBe('鉛筆（2）: ピッチを描く');
  await expect.poll(async () => (await menuBar()).find((m) => m.label === 'ノート').items
    .find((x) => x.label === '結合').accel).toBe('CmdOrCtrl+J');
});

test('エラーが出ていない', async () => {
  expect(errors).toEqual([]);
});
