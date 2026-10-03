// Gliss の VST3 + ARA 2 プラグイン（DAW のエディタ欄に置く WebView2。JUCE の WebBrowserComponent）で、
// 今の画面（main.js ほか）が使う `window.api` を作る。Electron の preload.cjs と同じ形（キーは同じ）で、
// 足したもの（mode・transport・preview・setCompare・hostState・restartEngine・on…）だけが違う。
//
// **クラシックのスクリプト**（ES モジュールではない）。C++ が `WebBrowserComponent::Options::withUserScript` で
// ページのスクリプトより前に差し込む。Electron では読み込まれない（Electron の動きは変わらない）。
//
// C++ との口は 3 つ（最終の仕様は scratchpad の r1-report.md と plugin/ の WebResources / GlissEditor）:
//   - ネイティブ関数（JS → C++、Promise）: JUCE 9.0.3 の `@juce-framework/webview` の `getNativeFunction(name)`。
//     JUCE が resource provider で配る `/juce/index.js` を **動的 import** して取る（`__juce__invoke` などの内部の形は真似しない）。
//   - イベント（C++ → JS）: `window.__JUCE__.backend.addEventListener(eventId, fn)`。ページより前に登録するので取りこぼさない。
//   - ファイルの読み出し: resource provider の `/fs/<encodeURIComponent(絶対パス)>` を `fetch`（作業場所の下だけ C++ が返す）。
// JS → C++ のイベントは `ui-ready`（ページの読み込みが済んだ）だけ。
(() => {
  'use strict';
  if (window.api && window.api.mode) return;            // 二重に読まない

  const JUCE = window.__JUCE__;
  const backend = JUCE && JUCE.backend;

  // ---------------------------------------------------------------- ネイティブ関数
  // JUCE の補助（/juce/index.js）が resource provider の根（https://juce.backend/）から配られる。
  // テストは window.__glissLoadJuce で差し替える（{ getNativeFunction } を返す Promise）
  const JUCE_URL = 'https://juce.backend/juce/index.js';
  const juceUrl = () => {
    try {
      const u = new URL('/juce/index.js', window.location.href);
      return /^https?:$/.test(u.protocol) ? u.href : JUCE_URL;
    } catch { return JUCE_URL; }
  };
  let juceModule = null;
  const loadJuce = () => {
    if (!juceModule) {
      juceModule = (typeof window.__glissLoadJuce === 'function' ? window.__glissLoadJuce() : import(juceUrl()))
        .catch((err) => { juceModule = null; throw err; });
    }
    return juceModule;
  };
  const natives = new Map();
  async function native(name, ...args) {
    const m = await loadJuce();
    let f = natives.get(name);
    if (!f) { f = m.getNativeFunction(name); natives.set(name, f); }
    return f(...args);
  }
  const fail = (err, tool) => ({ ok: false, error: String((err && err.message) || err), ...(tool ? { tool } : {}) });

  // ---------------------------------------------------------------- イベント（C++ → JS）
  const EVENTS = ['playhead', 'selection', 'session-changed', 'project-changed', 'cache', 'engine'];
  const listeners = Object.fromEntries(EVENTS.map((e) => [e, []]));
  const subscribe = (eventId) => (fn) => { if (typeof fn === 'function') listeners[eventId].push(fn); };
  if (backend && typeof backend.addEventListener === 'function') {
    for (const eventId of EVENTS) {
      backend.addEventListener(eventId, (payload) => {
        for (const fn of listeners[eventId].slice()) {
          try { fn(payload); } catch (err) { console.error(`[ara-bridge] ${eventId}:`, err); }
        }
      });
    }
  }

  // ---------------------------------------------------------------- ファイルの読み出し（/fs/）
  const fsUrl = (p) => `/fs/${encodeURIComponent(String(p))}`;
  // JUCE 9.0.3 の resource provider は状態コードを選べない（いつも 200）。C++ は読ませないものをこの MIME で返す
  const NOT_FOUND = 'application/x-gliss-not-found';
  async function fetchFs(p) {
    const res = await fetch(fsUrl(p));
    const type = (res.headers && typeof res.headers.get === 'function' && res.headers.get('content-type')) || '';
    if (!res.ok || String(type).startsWith(NOT_FOUND)) throw new Error(`読み取りを許していない場所: ${p}`);
    return res;
  }

  // ---------------------------------------------------------------- メニュー・タイトル（JS の中で完結）
  // main.mjs の menuItem / menuModel / menuItemAt と同じ規則。ロール・最近使ったの項目は捨てる
  const menu = { model: [], cmds: new Map(), listeners: [], menuClick: [] };
  const title = { doc: null, listeners: [] };

  function acceleratorLabel(a) {
    if (!a) return null;
    return String(a).split('+').map((p) => (/^(CmdOrCtrl|CommandOrControl|Control|Ctrl)$/i.test(p) ? 'Ctrl'
      : /^(Option|AltGr)$/i.test(p) ? 'Alt' : /^num(\d)$/.test(p) ? `Num${p.slice(3)}` : p)).join('+');
  }

  /** 区切りの整理（先頭・末尾・続きは詰める）。 */
  function tidy(items) {
    const out = [];
    for (const it of items) {
      if (it.type === 'separator' && (!out.length || out[out.length - 1].type === 'separator')) continue;
      out.push(it);
    }
    while (out.length && out[out.length - 1].type === 'separator') out.pop();
    return out;
  }

  function buildItems(list, prefix, cmds) {
    const out = [];
    for (const it of list || []) {
      if (!it || it.recent || it.role) continue;
      const id = prefix ? `${prefix}.${out.length}` : String(out.length);
      if (it.sep) { out.push({ id, type: 'separator', label: '', role: null, enabled: true, visible: true, checked: false,
        accelerator: null, accelLabel: null, toolTip: '', submenu: null }); continue; }
      if (it.submenu) {
        const sub = buildItems(it.submenu, id, cmds);
        if (!sub.length) continue;
        out.push({ id, type: 'submenu', label: it.label || '', role: null, enabled: true, visible: true, checked: false,
          accelerator: null, accelLabel: null, toolTip: '', submenu: sub });
        continue;
      }
      const check = 'checked' in it;
      cmds.set(id, it.cmd);
      out.push({ id, type: check ? 'checkbox' : 'normal', label: it.label || '', role: null, enabled: it.enabled !== false,
        visible: true, checked: check ? !!it.checked : false, accelerator: it.accelerator || null,
        accelLabel: acceleratorLabel(it.accelerator), toolTip: '', submenu: null });
    }
    return tidy(out);
  }

  function setAppMenu(tpl) {
    if (!Array.isArray(tpl)) return;
    const cmds = new Map();
    const model = [];
    for (const top of tpl) {
      const id = String(model.length);
      const sub = buildItems(top.submenu, id, cmds);
      if (!sub.length) continue;
      model.push({ id, type: 'submenu', label: top.label || '', role: null, enabled: true, visible: true, checked: false,
        accelerator: null, accelLabel: null, toolTip: '', submenu: sub });
    }
    menu.model = model;
    menu.cmds = cmds;
    for (const fn of menu.listeners.slice()) { try { fn(model); } catch (err) { console.error('[ara-bridge] onAppMenu:', err); } }
  }

  function findItem(id) {
    const parts = String(id ?? '').split('.');
    let items = menu.model;
    let it = null;
    for (let i = 0; i < parts.length; i++) {
      const want = parts.slice(0, i + 1).join('.');
      it = (items || []).find((x) => x.id === want) || null;
      if (!it) return null;
      items = it.submenu;
    }
    return it;
  }

  const docTitle = () => (title.doc && title.doc.name ? `${title.doc.name}${title.doc.dirty ? '*' : ''}` : '');
  const windowTitle = () => (title.doc && title.doc.name ? `${title.doc.name}${title.doc.dirty ? '*' : ''} — Gliss` : 'Gliss');
  const titleInfo = () => ({ title: windowTitle(), doc: docTitle() });

  // ---------------------------------------------------------------- 画面の中の小さな選択（譜面のトラック）
  function chooseTrack(tracks) {
    if (!Array.isArray(tracks) || !tracks.length) return Promise.resolve(null);
    const host = window.document && window.document.querySelector && window.document.querySelector('#mock');
    if (!host) return Promise.resolve(tracks[0].index);
    return new Promise((resolve) => {
      const doc = window.document;
      const bg = doc.createElement('div');
      bg.className = 'ai-bg';
      const box = doc.createElement('div');
      box.className = 'aid';
      box.setAttribute('role', 'dialog');
      box.setAttribute('aria-label', '読み込む歌詞のトラック');
      const head = doc.createElement('div');
      head.className = 'kh';
      head.textContent = '読み込む歌詞のトラック';
      box.append(head);
      const done = (v) => { bg.remove(); box.remove(); resolve(v); };
      for (const t of tracks) {
        const row = doc.createElement('div');
        row.className = 'cl';
        const name = doc.createElement('span');
        name.className = 'n';
        name.textContent = `${t.index + 1}: ${t.name}（${t.lyric_notes} 音符）`;
        const b = doc.createElement('button');
        b.className = 'pri';
        b.textContent = '選ぶ';
        b.addEventListener('click', () => done(t.index));
        row.append(name, b);
        box.append(row);
      }
      const cancel = doc.createElement('div');
      cancel.className = 'cl';
      const cb = doc.createElement('button');
      cb.textContent = 'キャンセル';
      cb.addEventListener('click', () => done(null));
      cancel.append(cb);
      box.append(cancel);
      bg.addEventListener('click', () => done(null));
      box.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(null); } });
      host.append(bg, box);
      const first = box.querySelector('button');
      if (first && first.focus) first.focus();
    });
  }

  // ---------------------------------------------------------------- window.api
  const noop = () => {};
  const none = async () => null;

  const api = {
    /** プラグインのモード（Electron では undefined）。画面は `window.api.mode === 'ara'` で分ける（ara.js の ARA）。 */
    mode: 'ara',
    /** 音は DAW が出す。 */
    muted: false,

    /** MCP ツールを呼ぶ（C++ がエンジンへ素通し。禁止のツールは C++ が `{ ok: false, error }` で断る）。 */
    call: async (name, args) => {
      try {
        const r = await native('engineCall', String(name), args || {});
        return r && typeof r === 'object' ? r : fail('エンジンの返事が空', name);
      } catch (err) { return fail(err, name); }
    },

    bootstrap: async () => {
      let b = null;
      try { b = await native('bootstrap'); } catch (err) { b = { engineReady: false, engineError: String((err && err.message) || err) }; }
      b = b && typeof b === 'object' ? b : {};
      return {
        take: null, guide: null, lyrics: null, guideLyrics: null, projectDir: null, project: null, fromArgs: false,
        keys: {}, grid: null, view: null, preview: undefined, modelSizes: {},
        ...b,
        mode: 'ara',
        muted: false,
        engineReady: !!b.engineReady,
        engineError: b.engineError || null,
        hostCanTransport: b.hostCanTransport !== false,
      };
    },
    // 解析モデル・アドオン・自動更新・AI とつなぐは単体アプリのもの（プラグインは重みを %LOCALAPPDATA%\Gliss\models で共有する）
    modelState: none, modelStart: none, modelCancel: none, onModelProgress: noop,
    addons: ({ state: none, start: none, cancel: none, remove: none, onProgress: noop }),
    pickFile: async (kind) => (kind === 'guide' ? native('pickFile', 'guide') : null),
    readJson: async (p) => (await fetchFs(p)).json(),
    readFile: async (p) => (await fetchFs(p)).arrayBuffer(),
    saveState: async (patch) => { try { return await native('saveState', patch || {}); } catch { return false; } },
    pushRecent: none,
    saveDialog: none,
    pickProject: none, saveProjectDialog: none,
    askSave: async () => 'cancel',
    setDoc: (doc) => {
      title.doc = doc || null;
      const t = titleInfo();
      for (const fn of title.listeners.slice()) { try { fn(t); } catch (err) { console.error('[ara-bridge] onTitle:', err); } }
    },
    closeNow: noop,
    onConfirmClose: noop,
    pathForFile: () => null,
    openLyricsFile: async () => native('pickFile', 'lyrics'),
    pickLyricsScore: async () => native('pickFile', 'score'),
    chooseLyricsTrack: (tracks) => chooseTrack(tracks),
    confirmAsrDownload: async (model) => {
      if (!model || typeof model !== 'object') return false;
      const message = [
        '聞き取り（音声認識）に使うモデルをダウンロードしますか？',
        `モデル: ${model.label || model.id}`,
        `大きさ: ${model.size_text || ''}`,
        `保存先: ${model.path || ''}`,
        `ライセンス: ${model.license || '不明'}（${model.license_url || ''}）`,
        model.upstream ? `由来: ${model.upstream}` : null,
        `取得元: ${model.source_url || ''}`,
        '取得は初回だけです。モデルは Gliss に同梱していません。',
      ].filter((l) => l !== null).join('\n');
      try {
        return !!(await native('confirm', { title: '聞き取りのモデルを取得', message, ok: 'ダウンロード', cancel: 'キャンセル' }));
      } catch { return false; }
    },
    reveal: async (p) => { try { return await native('reveal', String(p)); } catch { return false; } },
    copyText: async (text) => { try { return await native('copyText', String(text ?? '')); } catch { return false; } },
    ai: ({ status: none, addClaudeCode: none, addDesktop: none, copyConfig: none, setAllow: none }),
    updates: ({ state: none, check: none, download: none, apply: none, setPreferences: none, dismissNotice: none, onState: noop }),
    onConfirmUpdate: noop,
    screenshot: none,
    setAppMenu,
    consumeAlt: () => { window.dispatchEvent(new window.Event('gliss-alt-consumed')); },
    getMenu: async () => ({ menu: menu.model, ...titleInfo() }),
    menuClick: async (id) => {
      const it = findItem(id);
      if (!it || it.type === 'separator' || it.type === 'submenu' || it.enabled === false || it.visible === false) return false;
      if (it.type === 'checkbox') it.checked = !it.checked;
      const cmd = menu.cmds.get(String(id));
      for (const fn of menu.menuClick.slice()) { try { fn({ cmd }); } catch (err) { console.error('[ara-bridge] onMenu:', err); } }
      return true;
    },
    onAppMenu: (fn) => { if (typeof fn === 'function') menu.listeners.push(fn); },
    onTitle: (fn) => { if (typeof fn === 'function') title.listeners.push(fn); },
    onMenu: (fn) => { if (typeof fn === 'function') menu.menuClick.push(fn); },
    onProjectChanged: subscribe('project-changed'),
    onSessionChanged: subscribe('session-changed'),

    // ---- プラグインだけ
    /** DAW の再生の制御（ARA のホストの再生の制御）。op: 'play' | 'stop' | 'toggle' | 'seek' | 'loop'。
     * seek: { track_id, sec }（編集の秒）か { song_sec }。loop: { a, b, track_id? }（track_id があれば編集の秒、無ければソングの秒）か null（解除）。
     * 返り値 { ok, reason? }。ホストが再生の制御を持たなければ { ok: false, reason: 'no-controller' }。 */
    transport: async (op, arg) => {
      try { return (await native('transport', String(op), arg === undefined ? null : arg)) || { ok: false }; }
      catch (err) { return { ok: false, reason: String((err && err.message) || err) }; }
    },
    /** つかんだノートのプレビュー音（EditorRenderer）。op: 'start'（{ path, loop }）| 'stop'。 */
    preview: async (op, arg) => {
      try { return (await native('preview', String(op), arg === undefined ? null : arg)) || { ok: false }; }
      catch (err) { return { ok: false, reason: String((err && err.message) || err) }; }
    },
    /** 原音と比べる（キャッシュを読まずに原音を返す）。ドキュメント全体のフラグ。 */
    setCompare: async (on) => { try { return await native('setCompare', !!on); } catch { return false; } },
    /** 今の選択・再生位置・トラックの状態（イベントを取りこぼした後の引き直し）。 */
    hostState: async () => {
      try {
        const h = await native('hostState');
        return h && typeof h === 'object' ? h : { selection: null, playhead: null, tracks: [], engine: { state: 'ready' } };
      } catch { return { selection: null, playhead: null, tracks: [], engine: { state: 'failed', error: 'ホストの状態を読めない' } }; }
    },
    restartEngine: async () => { try { return await native('restartEngine'); } catch (err) { return fail(err); } },
    onPlayhead: subscribe('playhead'),
    onSelection: subscribe('selection'),
    onCacheState: subscribe('cache'),
    onEngineState: subscribe('engine'),
  };

  window.api = api;

  // CSS の分岐（index.html の html[data-mode=ara]）。文書がまだ無い（ドキュメント生成の直後）ときは作られてから
  const markMode = () => { if (window.document && window.document.documentElement) window.document.documentElement.dataset.mode = 'ara'; };
  markMode();
  if (window.document && window.document.addEventListener) window.document.addEventListener('DOMContentLoaded', markMode);

  // ページの読み込みが済んだ（モジュールのスクリプトは済んでいる）: C++ は画面が出てからイベントを送り始める
  const uiReady = () => {
    if (backend && typeof backend.emitEvent === 'function') backend.emitEvent('ui-ready', {});
  };
  if (window.document && window.document.readyState === 'complete') uiReady();
  else window.addEventListener('load', uiReady);
})();
