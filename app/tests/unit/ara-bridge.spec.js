// プラグイン（VST3 + ARA 2）の画面の橋 renderer/ara-bridge.js。Electron は起動しない（Node の vm で、偽の window.__JUCE__・fetch を渡して読む）。
//
//   (B1) window.api のキーは preload.cjs と同じ（足したもの 10 個だけが違う）。mode は 'ara'
//   (B2) call は native の engineCall に行く。失敗・空の返事は { ok:false, error, tool } で返す（Electron の engine.call と同じ形）
//   (B3) readFile・readJson は /fs/<encodeURIComponent(パス)> を fetch する。許されない（404）は main と同じ文言で落とす
//   (B4) bootstrap: native の返り値に既定を足す（keys・grid・modelSizes…）。hostCanTransport は false と言われたときだけ false
//   (B5) メニュー: setAppMenu のテンプレート → getMenu の形（main.mjs の menuModel と同じ）。ロール・最近使ったは捨てる。
//        区切りは詰める。menuClick → onMenu { cmd }。無効・区切り・下の段を持つ項目は押せない。onAppMenu に知らせる
//   (B6) タイトル: setDoc → onTitle と getMenu の title / doc（未保存の * ）
//   (B7) C++ のイベント（playhead・selection・session-changed・project-changed・cache・engine）→ on… の受け手
//   (B8) transport・preview・setCompare・hostState・restartEngine は native に行く。失敗しても落ちない
//   (B9) pickFile は 'guide' だけ native へ（'track' は無し）。歌詞・譜面・確認・コピー・開くも native へ
//   (B10) 画面の中の小さな選択（譜面のトラック）
//   (B11) 起動: html に data-mode="ara"・ui-ready を C++ へ（ページの読み込みが済んでから）
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.resolve(HERE, '..', '..');
const BRIDGE = fs.readFileSync(path.join(APP, 'renderer', 'ara-bridge.js'), 'utf8');
const PRELOAD = fs.readFileSync(path.join(APP, 'preload.cjs'), 'utf8');

/** 偽の DOM（chooseLyricsTrack 用）。 */
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.attrs = {}; this.className = ''; this.textContent = ''; this.dataset = {}; this.removed = false; }
  append(...c) { this.children.push(...c); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  remove() { this.removed = true; }
  focus() {}
  fire(t, e = {}) { for (const fn of this.listeners[t] || []) fn(e); }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelector(sel) { return this.all().find((c) => c.tag === sel) || null; }
}

/** ブリッジを読む。natives: native 関数の名前 → 実装。files: /fs/ の中身（無ければ 404）。 */
function load({ natives = {}, files = {}, readyState = 'loading', dom = false } = {}) {
  const calls = [];
  const handlers = new Map();
  const emitted = [];
  const winListeners = {};
  const docListeners = {};
  const mock = new El('div');
  const win = {
    console,
    URL,
    encodeURIComponent,
    JSON,
    Promise,
    Event: class { constructor(type) { this.type = type; } },
    location: { href: 'https://juce.backend/' },
    __JUCE__: {
      initialisationData: { __juce__functions: [] },
      backend: {
        addEventListener: (id, fn) => { (handlers.get(id) || handlers.set(id, []).get(id)).push(fn); },
        emitEvent: (id, payload) => { emitted.push([id, payload]); },
      },
    },
    __glissLoadJuce: async () => ({
      getNativeFunction: (name) => async (...args) => {
        calls.push([name, ...args]);
        const f = natives[name];
        return f ? f(...args) : null;
      },
    }),
    fetch: async (url) => {
      calls.push(['fetch', url]);
      const entry = files[url];
      const body = entry && typeof entry === 'object' ? entry.body : entry;
      const type = entry && typeof entry === 'object' ? entry.type : 'application/json';
      return { ok: body !== undefined, status: body !== undefined ? 200 : 404,
        headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? type : null) },
        json: async () => JSON.parse(body), arrayBuffer: async () => new TextEncoder().encode(body).buffer };
    },
    addEventListener: (t, fn) => { (winListeners[t] ||= []).push(fn); },
    dispatchEvent: (e) => { calls.push(['dispatchEvent', e.type]); },
    document: {
      readyState,
      documentElement: { dataset: {} },
      addEventListener: (t, fn) => { (docListeners[t] ||= []).push(fn); },
      createElement: (tag) => new El(tag),
      querySelector: (sel) => (dom && sel === '#mock' ? mock : null),
    },
  };
  win.window = win;
  vm.createContext(win);
  new vm.Script(BRIDGE, { filename: 'ara-bridge.js' }).runInContext(win);
  return {
    win, api: win.api, calls, emitted, mock,
    /** C++ からのイベント（emitByBackend が JSON を解いて渡す形）。 */
    fire: (id, payload) => { for (const fn of handlers.get(id) || []) fn(payload); },
    fireWindow: (t) => { for (const fn of winListeners[t] || []) fn({}); },
    fireDocument: (t) => { for (const fn of docListeners[t] || []) fn({}); },
    natives: () => calls.filter((c) => c[0] !== 'fetch' && c[0] !== 'dispatchEvent'),
  };
}

/** preload.cjs の window.api のキー（exposeInMainWorld の第 2 引数の最上位）。 */
function preloadKeys() {
  const body = PRELOAD.slice(PRELOAD.indexOf("exposeInMainWorld('api', {"));
  return [...body.matchAll(/^ {2}(\w+):/gm)].map((m) => m[1]).filter((k) => k !== 'araTest');
}

const ADDED = ['mode', 'transport', 'preview', 'setCompare', 'hostState', 'restartEngine',
  'onPlayhead', 'onSelection', 'onCacheState', 'onEngineState'];

test('(B1) window.api のキーは preload.cjs と同じ（足したものだけ違う）', () => {
  const { api } = load();
  const pre = preloadKeys();
  expect(pre.length).toBeGreaterThan(30);
  expect(Object.keys(api).filter((k) => !ADDED.includes(k)).sort()).toEqual([...pre].sort());
  for (const k of ADDED) expect(api[k], k).toBeDefined();
  expect(api.mode).toBe('ara');
  expect(api.muted).toBe(false);
  // ネストしたもの（addons・ai・updates）も同じ関数を持つ（呼ばれても落ちない）
  for (const group of ['addons', 'ai', 'updates']) expect(typeof api[group]).toBe('object');
  for (const k of ['state', 'start', 'cancel', 'remove', 'onProgress']) expect(typeof api.addons[k]).toBe('function');
  for (const k of ['status', 'addClaudeCode', 'addDesktop', 'copyConfig', 'setAllow']) expect(typeof api.ai[k]).toBe('function');
  for (const k of ['state', 'check', 'download', 'apply', 'setPreferences', 'dismissNotice', 'onState']) expect(typeof api.updates[k]).toBe('function');
});

test('(B2) call は engineCall に行き、失敗は { ok:false } で返す', async () => {
  const h = load({ natives: { engineCall: async (name, args) => (name === 'boom' ? Promise.reject(new Error('落ちた')) : name === 'empty' ? null : { ok: true, name, args }) } });
  expect(await h.api.call('list_tracks', { a: 1 })).toEqual({ ok: true, name: 'list_tracks', args: { a: 1 } });
  expect(await h.api.call('list_tracks')).toEqual({ ok: true, name: 'list_tracks', args: {} });
  expect(await h.api.call('boom', {})).toEqual({ ok: false, error: '落ちた', tool: 'boom' });
  expect((await h.api.call('empty', {})).ok).toBe(false);
  expect(h.natives()[0]).toEqual(['engineCall', 'list_tracks', { a: 1 }]);
});

test('(B3) readFile・readJson は /fs/ を fetch する', async () => {
  const p = 'D:\\Gliss\\work\\ara\\k1\\view data.json';
  const url = `/fs/${encodeURIComponent(p)}`;
  expect(url).toBe('/fs/D%3A%5CGliss%5Cwork%5Cara%5Ck1%5Cview%20data.json');
  const h = load({ files: { [url]: '{"a":[1,2]}' } });
  expect(await h.api.readJson(p)).toEqual({ a: [1, 2] });
  expect(new TextDecoder().decode(await h.api.readFile(p))).toBe('{"a":[1,2]}');
  expect(h.calls.filter((c) => c[0] === 'fetch').map((c) => c[1])).toEqual([url, url]);
  // vm の中の Promise・Error は別の realm（expect の rejects に直接は渡せない）
  const failure = (pr) => Promise.resolve(pr).then(() => null, (e) => e.message);
  expect(await failure(h.api.readFile('C:\\Windows\\win.ini'))).toBe('読み取りを許していない場所: C:\\Windows\\win.ini');
  expect(await failure(h.api.readJson('C:\\x.json'))).toBe('読み取りを許していない場所: C:\\x.json');
  // C++（JUCE の resource provider）は 404 を返せないので、読ませないものは 200 と印の MIME で来る
  const denied = 'D:\\outside.json';
  const h2 = load({ files: { [`/fs/${encodeURIComponent(denied)}`]: { body: 'not found', type: 'application/x-gliss-not-found' } } });
  expect(await failure(h2.api.readJson(denied))).toBe(`読み取りを許していない場所: ${denied}`);
  expect(await failure(h2.api.readFile(denied))).toBe(`読み取りを許していない場所: ${denied}`);
});

test('(B4) bootstrap は既定を足す', async () => {
  const h = load({ natives: { bootstrap: async () => ({ version: '0.1.0', engineReady: true, keys: { undo: ['Ctrl+Z'] }, selection: { track_id: 't1' }, hostCanTransport: false, fileGuide: true }) } });
  const b = await h.api.bootstrap();
  expect(b).toMatchObject({ mode: 'ara', muted: false, engineReady: true, engineError: null, keys: { undo: ['Ctrl+Z'] }, grid: null, view: null,
    modelSizes: {}, take: null, project: null, hostCanTransport: false, fileGuide: true, selection: { track_id: 't1' } });
  const h2 = load({ natives: { bootstrap: async () => ({ engineReady: true }) } });
  expect((await h2.api.bootstrap()).hostCanTransport).toBe(true);
  const h3 = load({ natives: { bootstrap: async () => { throw new Error('起動できない'); } } });
  expect(await h3.api.bootstrap()).toMatchObject({ engineReady: false, engineError: '起動できない', hostCanTransport: true });
});

// commands.js の appMenuTemplate と同じ形
const TPL = [
  { label: 'ファイル', submenu: [
    { cmd: 'open-guide', label: 'ガイドを開く…', accelerator: null, enabled: true },
    { sep: true },
    { recent: true, label: '最近使ったプロジェクト' },
    { sep: true },
    { cmd: 'load-lyrics', label: '歌詞を読み込む（テキスト）…', accelerator: null, enabled: false },
    { sep: true },
    { role: 'quit', label: '終了' },
  ] },
  { label: '編集', submenu: [
    { cmd: 'undo', label: '元に戻す', accelerator: 'Ctrl+Z', enabled: true },
    { cmd: 'redo', label: 'やり直す', accelerator: 'CmdOrCtrl+Shift+Z', enabled: false },
    { sep: true },
    { label: 'ピッチ検出の方式', submenu: [
      { cmd: 'f0-rmvpe', label: 'RMVPE（既定）', accelerator: null, enabled: true, checked: true },
      { cmd: 'f0-gliss', label: 'Gliss（試作）', accelerator: null, enabled: true, checked: false },
    ] },
  ] },
  { label: '空', submenu: [{ role: 'about', label: 'Gliss について' }] },
];

test('(B5) メニュー: テンプレート → getMenu・menuClick → onMenu', async () => {
  const h = load();
  const seen = [];
  const clicks = [];
  h.api.onAppMenu((m) => seen.push(m));
  h.api.onMenu((m) => clicks.push(m));
  expect((await h.api.getMenu()).menu).toEqual([]);
  h.api.setAppMenu(TPL);
  expect(seen).toHaveLength(1);
  const { menu, title, doc } = await h.api.getMenu();
  expect(title).toBe('Gliss');
  expect(doc).toBe('');
  // 上の段: ロールだけの「空」は消える。下の段: ロール・最近使ったは捨て、続く・端の区切りは詰める
  expect(menu.map((m) => m.label)).toEqual(['ファイル', '編集']);
  const file = menu[0].submenu;
  expect(file.map((i) => (i.type === 'separator' ? '-' : i.label))).toEqual(['ガイドを開く…', '-', '歌詞を読み込む（テキスト）…']);
  expect(file[0]).toMatchObject({ id: '0.0', type: 'normal', enabled: true, checked: false, visible: true, role: null, submenu: null });
  expect(file[2]).toMatchObject({ id: '0.3', enabled: false });   // id は項目の位置（捨てた項目の分は詰める）
  const edit = menu[1].submenu;
  expect(edit[0]).toMatchObject({ id: '1.0', label: '元に戻す', accelerator: 'Ctrl+Z', accelLabel: 'Ctrl+Z' });
  expect(edit[1]).toMatchObject({ accelerator: 'CmdOrCtrl+Shift+Z', accelLabel: 'Ctrl+Shift+Z', enabled: false });
  expect(edit[3]).toMatchObject({ type: 'submenu', label: 'ピッチ検出の方式', accelLabel: null });
  expect(edit[3].submenu[0]).toMatchObject({ id: '1.3.0', type: 'checkbox', checked: true });
  expect(edit[3].submenu[1]).toMatchObject({ id: '1.3.1', type: 'checkbox', checked: false });
  // 押す
  expect(await h.api.menuClick('1.0')).toBe(true);
  expect(await h.api.menuClick('1.3.1')).toBe(true);
  expect(clicks).toEqual([{ cmd: 'undo' }, { cmd: 'f0-gliss' }]);
  // チェックは押した項目だけ反転（描き直しの setAppMenu までの見かけ）
  const m2 = (await h.api.getMenu()).menu;
  expect(m2[1].submenu[3].submenu.map((i) => i.checked)).toEqual([true, true]);
  // 押せない: 無効・区切り・下の段を持つ項目・存在しない
  expect(await h.api.menuClick('1.1')).toBe(false);
  expect(await h.api.menuClick('0.3')).toBe(false);
  expect(await h.api.menuClick('0.1')).toBe(false);
  expect(await h.api.menuClick('1.3')).toBe(false);
  expect(await h.api.menuClick('9.9')).toBe(false);
  expect(clicks).toHaveLength(2);
  // テンプレートを送り直すと作り直す（id は位置）
  h.api.setAppMenu([{ label: 'ヘルプ', submenu: [{ cmd: 'keys', label: 'ショートカット…', accelerator: 'Ctrl+,', enabled: true }] }]);
  expect(seen).toHaveLength(2);
  expect((await h.api.getMenu()).menu[0].submenu[0]).toMatchObject({ id: '0.0', accelLabel: 'Ctrl+,' });
  expect(await h.api.menuClick('0.0')).toBe(true);
  expect(clicks.at(-1)).toEqual({ cmd: 'keys' });
  h.api.setAppMenu('壊れた');         // 配列でなければ何もしない
  expect(seen).toHaveLength(2);
});

test('(B6) タイトル: setDoc → onTitle・getMenu', async () => {
  const h = load();
  const titles = [];
  h.api.onTitle((t) => titles.push(t));
  h.api.setDoc({ name: 'Song 1', dirty: false, kind: 'ara' });
  h.api.setDoc({ name: 'Song 1', dirty: true, kind: 'gliss' });
  h.api.setDoc(null);
  expect(titles).toEqual([{ title: 'Song 1 — Gliss', doc: 'Song 1' }, { title: 'Song 1* — Gliss', doc: 'Song 1*' }, { title: 'Gliss', doc: '' }]);
  h.api.setDoc({ name: 'Song 2', dirty: false });
  expect(await h.api.getMenu()).toMatchObject({ title: 'Song 2 — Gliss', doc: 'Song 2' });
});

test('(B7) C++ のイベント → on… の受け手', () => {
  const h = load();
  const got = {};
  h.api.onPlayhead((e) => (got.playhead = e));
  h.api.onSelection((e) => (got.selection = e));
  h.api.onCacheState((e) => (got.cache = e));
  h.api.onEngineState((e) => (got.engine = e));
  h.api.onSessionChanged((e) => (got.session = e));
  h.api.onProjectChanged((e) => (got.project = e));
  const second = [];
  h.api.onPlayhead((e) => second.push(e));
  h.api.onPlayhead(() => { throw new Error('受け手の失敗は他に回さない'); });
  h.api.onPlayhead(null);                       // 関数でなければ無視
  const ev = {
    playhead: { song_sec: 1.5, playing: true, loop: null, mapped: { t1: 0.5 } },
    selection: { track_id: 't1', ara_id: 'a1', region: { id: 'r1', song_start: 1, song_end: 3, mod_start: 0, mod_end: 2 } },
    cache: { track_id: 't1', state: 'reading', progress: 0.4 },
    engine: { state: 'failed', error: '落ちた' },
    'session-changed': { dir: 'D:\\w' },
    'project-changed': { track_id: 't1' },
  };
  const origError = console.error;
  console.error = () => {};
  try { for (const [id, p] of Object.entries(ev)) h.fire(id, p); } finally { console.error = origError; }
  expect(got).toEqual({ playhead: ev.playhead, selection: ev.selection, cache: ev.cache, engine: ev.engine, session: ev['session-changed'], project: ev['project-changed'] });
  expect(second).toEqual([ev.playhead]);
});

test('(B8) transport・preview・setCompare・hostState・restartEngine は native に行く', async () => {
  const h = load({ natives: {
    transport: async (op, arg) => (op === 'seek' && arg?.track_id === 'x' ? { ok: false, reason: 'no-controller' } : { ok: true }),
    preview: async () => ({ ok: true }), setCompare: async (on) => on, restartEngine: async () => ({ ok: true }),
    hostState: async () => ({ selection: null, playhead: null, tracks: [{ track_id: 't1', regions: [] }], engine: { state: 'ready' } }),
  } });
  expect(await h.api.transport('toggle')).toEqual({ ok: true });
  expect(await h.api.transport('seek', { song_sec: 2 })).toEqual({ ok: true });
  expect(await h.api.transport('seek', { track_id: 'x', sec: 1 })).toEqual({ ok: false, reason: 'no-controller' });
  expect(await h.api.transport('loop', null)).toEqual({ ok: true });
  expect(await h.api.preview('start', { path: 'D:\\a.wav', loop: true })).toEqual({ ok: true });
  expect(await h.api.setCompare(true)).toBe(true);
  expect((await h.api.hostState()).tracks[0].track_id).toBe('t1');
  expect(await h.api.restartEngine()).toEqual({ ok: true });
  expect(h.natives().map((c) => c.slice(0, 3))).toEqual([
    ['transport', 'toggle', null], ['transport', 'seek', { song_sec: 2 }], ['transport', 'seek', { track_id: 'x', sec: 1 }],
    ['transport', 'loop', null], ['preview', 'start', { path: 'D:\\a.wav', loop: true }], ['setCompare', true], ['hostState'], ['restartEngine'],
  ]);
  // native が落ちても例外にしない
  const boom = async () => { throw new Error('C++ が答えない'); };
  const h2 = load({ natives: { transport: boom, preview: boom, hostState: boom, setCompare: boom, restartEngine: boom } });
  expect(await h2.api.transport('play')).toEqual({ ok: false, reason: 'C++ が答えない' });
  expect(await h2.api.hostState()).toMatchObject({ selection: null, tracks: [], engine: { state: 'failed' } });
  expect(await h2.api.setCompare(true)).toBe(false);
  expect(await h2.api.restartEngine()).toMatchObject({ ok: false });
});

test('(B9) ファイルを選ぶ・確認・コピー・開くは native へ。単体アプリのものは何もしない', async () => {
  const h = load({ natives: {
    pickFile: async (kind) => ({ guide: 'D:\\g.wav', lyrics: { path: 'D:\\l.txt', text: 'あいう' }, score: 'D:\\s.svp' })[kind] ?? null,
    confirm: async () => true, copyText: async () => true, reveal: async () => true, saveState: async () => true,
  } });
  expect(await h.api.pickFile('guide')).toBe('D:\\g.wav');
  expect(await h.api.pickFile('track')).toBeNull();
  expect(await h.api.pickFile()).toBeNull();
  expect(await h.api.openLyricsFile()).toEqual({ path: 'D:\\l.txt', text: 'あいう' });
  expect(await h.api.pickLyricsScore()).toBe('D:\\s.svp');
  expect(await h.api.confirmAsrDownload({ id: 'small', label: 'Whisper small', size_text: '460 MB', path: 'D:\\m', license: 'MIT', license_url: 'u', source_url: 's' })).toBe(true);
  expect(await h.api.confirmAsrDownload(null)).toBe(false);
  expect(await h.api.copyText('x')).toBe(true);
  expect(await h.api.reveal('D:\\w\\a.wav')).toBe(true);
  expect(await h.api.saveState({ view: { t0: 0 } })).toBe(true);
  const conf = h.natives().find((c) => c[0] === 'confirm');
  expect(conf[1]).toMatchObject({ title: '聞き取りのモデルを取得', ok: 'ダウンロード', cancel: 'キャンセル' });
  expect(conf[1].message).toContain('Whisper small');
  expect(conf[1].message).toContain('460 MB');
  // 単体アプリのもの
  expect(await h.api.pathForFile({})).toBeNull();
  expect(await h.api.saveDialog()).toBeNull();
  expect(await h.api.pickProject()).toBeNull();
  expect(await h.api.askSave('x')).toBe('cancel');
  expect(await h.api.pushRecent({})).toBeNull();
  expect(await h.api.updates.check()).toBeNull();
  expect(await h.api.addons.state()).toBeNull();
  expect(await h.api.ai.status()).toBeNull();
  // native が落ちても例外にしない
  const h2 = load({ natives: { copyText: async () => { throw new Error('x'); }, confirm: async () => { throw new Error('x'); }, saveState: async () => { throw new Error('x'); } } });
  expect(await h2.api.copyText('a')).toBe(false);
  expect(await h2.api.confirmAsrDownload({ id: 'a' })).toBe(false);
  expect(await h2.api.saveState({})).toBe(false);
  // Alt を離してもメニューバーへ行かない（Event を出すだけ）
  h.api.consumeAlt();
  expect(h.calls.filter((c) => c[0] === 'dispatchEvent')).toEqual([['dispatchEvent', 'gliss-alt-consumed']]);
});

test('(B10) 譜面のトラックを選ぶ小さなリスト', async () => {
  const tracks = [{ index: 2, name: 'Vocal', lyric_notes: 40 }, { index: 5, name: 'Harmony', lyric_notes: 12 }];
  // 画面が無ければ 1 本目
  expect(await load().api.chooseLyricsTrack(tracks)).toBe(2);
  expect(await load().api.chooseLyricsTrack([])).toBeNull();
  // 画面（#mock）があればボタンで選ぶ
  const h = load({ dom: true });
  const p = h.api.chooseLyricsTrack(tracks);
  const buttons = h.mock.all().filter((c) => c.tag === 'button');
  expect(buttons.map((b) => b.textContent)).toEqual(['選ぶ', '選ぶ', 'キャンセル']);
  buttons[1].fire('click');
  expect(await p).toBe(5);
  expect(h.mock.children.every((c) => c.removed)).toBe(true);
  const q = h.api.chooseLyricsTrack(tracks);
  h.mock.all().filter((c) => c.tag === 'button').at(-1).fire('click');
  expect(await q).toBeNull();
});

test('(B11) 起動: data-mode・ui-ready', () => {
  const loading = load({ readyState: 'loading' });
  expect(loading.win.document.documentElement.dataset.mode).toBe('ara');
  expect(loading.emitted).toEqual([]);          // 読み込みが済む前は送らない
  loading.fireWindow('load');
  expect(loading.emitted).toEqual([['ui-ready', {}]]);
  const done = load({ readyState: 'complete' });
  expect(done.emitted).toEqual([['ui-ready', {}]]);
  // 二重に読んでも window.api を作り直さない（C++ が 2 回差し込んだ・フレームごと）
  const api = loading.win.api;
  vm.runInContext(BRIDGE, loading.win);
  expect(loading.win.api).toBe(api);
  // JUCE の外（普通のブラウザ）でも落ちない
  const bare = vm.createContext({ console, URL, encodeURIComponent, Promise, JSON, addEventListener() {}, location: { href: 'about:blank' } });
  bare.window = bare;
  vm.runInContext(BRIDGE, bare);
  expect(bare.api.mode).toBe('ara');
});
