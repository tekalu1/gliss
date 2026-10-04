// preload。renderer には「エンジンのツールを呼ぶ」ことしか渡さない。
// renderer からエンジンの内部関数は呼べない（画面も AI も同じ MCP ツールを通す）。
const { contextBridge, ipcRenderer, webUtils } = require('electron');

// 試験の口（GLISS_TEST_ARA=1 のときだけ）: Electron の画面を「プラグインのモード」（window.api.mode === 'ara'。tests/ara-mode.spec.js）で起動する。
// 本物の DAW・C++ の代わりに、DAW の再生の制御（transport・preview・setCompare）の呼び出しを記録し、C++ のイベント（playhead・selection・
// cache・engine・session-changed・project-changed）を __araEmit で差し込む。本番のプラグインでは ara-bridge.js が同じ形の window.api を作る。
const TEST_ARA = process.env.GLISS_TEST_ARA === '1';
const araCalls = [];
const araHost = { selection: null, playhead: null, tracks: [], engine: { state: 'ready' } };
const araListeners = { playhead: [], selection: [], cache: [], engine: [], 'session-changed': [], 'project-changed': [] };
const araOn = (name) => (fn) => { araListeners[name].push(fn); };
const araTest = TEST_ARA ? {
  mode: 'ara',
  transport: async (op, arg) => { araCalls.push({ kind: 'transport', op, arg: arg ?? null }); return { ok: true }; },
  preview: async (op, arg) => { araCalls.push({ kind: 'preview', op, arg: arg ?? null }); return { ok: true }; },
  setCompare: async (on) => { araCalls.push({ kind: 'setCompare', on: !!on }); return true; },
  hostState: async () => JSON.parse(JSON.stringify(araHost)),
  restartEngine: async () => { araCalls.push({ kind: 'restartEngine' }); return { ok: true }; },
  onPlayhead: araOn('playhead'),
  onSelection: araOn('selection'),
  onCacheState: araOn('cache'),
  onEngineState: araOn('engine'),
  /** C++ のイベントを差し込む（session-changed・project-changed も）。 */
  __araEmit: (name, data) => { for (const fn of araListeners[name] || []) fn(data); },
  __araCalls: () => araCalls.map((c) => ({ ...c })),
  __araClear: () => { araCalls.length = 0; },
  /** hostState() が返す値を変える（regions・cache・engine・selection）。 */
  __araSetHost: (patch) => { Object.assign(araHost, patch); },
} : {};

contextBridge.exposeInMainWorld('api', {
  /** 音を出さない起動（テスト。`--mute` か VOCAL_EDITOR_MUTE=1。main が環境変数に入れる）。
   * renderer は出力の音量を 0 にする（Chromium の --mute-audio・setAudioMuted と三重。issue #27）。 */
  muted: process.env.VOCAL_EDITOR_MUTE === '1',

  /** MCP ツールを呼ぶ。名前と引数はエンジンの docs/MCP.md のとおり。 */
  call: (name, args) => ipcRenderer.invoke('engine.call', name, args),

  bootstrap: () => ipcRenderer.invoke('app.bootstrap'),
  modelState: () => ipcRenderer.invoke('app.modelState'),
  modelStart: (ids) => ipcRenderer.invoke('app.modelStart', ids),
  modelCancel: () => ipcRenderer.invoke('app.modelCancel'),
  onModelProgress: (fn) => ipcRenderer.on('app.modelProgress', (_e, progress) => fn(progress)),
  /** 任意機能のアドオン（ヘルプ > モデルと追加の機能…。main の addons.mjs）。 */
  addons: {
    state: () => ipcRenderer.invoke('app.addonsState'),
    start: (id) => ipcRenderer.invoke('app.addonStart', id),
    cancel: () => ipcRenderer.invoke('app.addonCancel'),
    remove: (id) => ipcRenderer.invoke('app.addonRemove', id),
    onProgress: (fn) => ipcRenderer.on('app.addonProgress', (_e, progress) => fn(progress)),
  },
  pickFile: (kind) => ipcRenderer.invoke('app.pickFiles', kind),
  readJson: (path) => ipcRenderer.invoke('app.readJson', path),
  readFile: (path) => ipcRenderer.invoke('app.readFile', path),
  saveState: (patch) => ipcRenderer.invoke('app.saveState', patch),
  pushRecent: (entry) => ipcRenderer.invoke('app.pushRecent', entry),
  saveDialog: (defaultPath) => ipcRenderer.invoke('app.saveDialog', defaultPath),
  // プロジェクトのファイル（issue #33）: 開く・名前を付けて保存のダイアログ、「保存しますか」、タイトル（名前・未保存）
  pickProject: () => ipcRenderer.invoke('app.pickProject'),
  saveProjectDialog: (defaultPath) => ipcRenderer.invoke('app.saveProjectDialog', defaultPath),
  askSave: (name) => ipcRenderer.invoke('app.askSave', name),
  setDoc: (doc) => ipcRenderer.send('app.setDoc', doc),
  closeNow: () => ipcRenderer.send('app.closeNow'),
  /** ウィンドウを閉じようとしたが保存していない変更がある（renderer が「保存しますか」を聞く）。 */
  onConfirmClose: (fn) => { ipcRenderer.on('confirm-close', () => fn()); },
  /** ドロップされたファイル（File）のパス。Electron 32 から File.path が無いので webUtils で引く。 */
  pathForFile: (file) => { try { return webUtils.getPathForFile(file) || null; } catch { return null; } },
  openLyricsFile: () => ipcRenderer.invoke('app.openLyricsFile'),
  pickLyricsScore: () => ipcRenderer.invoke('app.pickLyricsScore'),
  chooseLyricsTrack: (tracks) => ipcRenderer.invoke('app.chooseLyricsTrack', tracks),
  /** 聞き取り（音声認識。issue #54）のモデルを初めて取得する前の確認（大きさ・保存先・ライセンス）。true = 取得する。 */
  confirmAsrDownload: (model) => ipcRenderer.invoke('app.confirmAsrDownload', model),
  reveal: (p) => ipcRenderer.invoke('app.reveal', p),
  /** 文字をクリップボードへ（右クリックの「AI に頼む」）。 */
  copyText: (text) => ipcRenderer.invoke('app.copyText', text),
  /** AI とつなぐ（ヘルプ > AI とつなぐ…）: 状態・Claude Code / Claude Desktop に登録・設定をコピー・AI に許可。 */
  ai: {
    status: () => ipcRenderer.invoke('ai.status'),
    addClaudeCode: () => ipcRenderer.invoke('ai.addClaudeCode'),
    addDesktop: () => ipcRenderer.invoke('ai.addDesktop'),
    copyConfig: () => ipcRenderer.invoke('ai.copyConfig'),
    setAllow: (patch) => ipcRenderer.invoke('ai.setAllow', patch),
  },
  /** 自動更新（ヘルプ > 更新を確認…。main の updates.mjs）。状態は { phase, version, target, progress, error,
   * channel, autoCheck, autoDownload, updatedFrom, enabled }（state() は notes＝更新の内容も付く）。 */
  updates: {
    state: () => ipcRenderer.invoke('updates.state'),
    check: () => ipcRenderer.invoke('updates.check'),
    download: () => ipcRenderer.invoke('updates.download'),
    apply: () => ipcRenderer.invoke('updates.apply'),
    setPreferences: (value) => ipcRenderer.invoke('updates.setPreferences', value),
    dismissNotice: () => ipcRenderer.invoke('updates.dismissNotice'),
    onState: (fn) => { ipcRenderer.on('updates-state', (_e, s) => fn(s)); },
  },
  /** 再起動して更新の前に「保存しますか」を通す。fn は閉じてよければ true（キャンセルなら false）を返す。 */
  onConfirmUpdate: (fn) => {
    ipcRenderer.on('confirm-update', async (_e, id) => {
      let ok = false;
      try { ok = !!(await fn()); } finally { ipcRenderer.send('app.answer', id, ok); }
    });
  },
  screenshot: (name) => ipcRenderer.invoke('app.screenshot', name),
  /** メニューバーの並び（名前・キー・有効・チェック。コマンドの表から作る。issue #17・#22）。
   * 編集メニューの「元に戻す: ○○」（issue #16）もここに入っている。 */
  setAppMenu: (tpl) => ipcRenderer.send('app.setAppMenu', tpl),
  /** Alt+ドラッグに Alt を使った（離したときにメニューバーへ行かないように）。main は離した keyUp を止め、
   * 画面のメニューバー（titlebar.js）にもその場で知らせる（main へ届くより先に keyUp が来ることがある）。 */
  consumeAlt: () => {
    window.dispatchEvent(new Event('gliss-alt-consumed'));
    ipcRenderer.send('app.consumeAlt');
  },

  /** タイトルバーのメニューバー（titlebar.js）: main の Menu を写したもの・中央の名前を読む。
   * { menu: [{ id, type, label, enabled, checked, accelLabel, submenu }], title, doc } */
  getMenu: () => ipcRenderer.invoke('app.getMenu'),
  /** メニューの項目（id = 項目の位置）を押す。main が Menu の同じ項目の click を呼ぶ。 */
  menuClick: (id) => ipcRenderer.invoke('app.menuClick', id),
  /** main が Menu を作り直したとき（中身が変わったとき）に呼ばれる。 */
  onAppMenu: (fn) => { ipcRenderer.on('app-menu', (_e, m) => fn(m)); },
  /** ウィンドウのタイトルが変わったとき（{ title, doc }。doc は中央に出す名前）。 */
  onTitle: (fn) => { ipcRenderer.on('app-title', (_e, t) => fn(t)); },

  /** メニュー（ファイル／編集／ノート／表示）が押されたときに呼ばれる。{ cmd: コマンドの id } */
  onMenu: (fn) => {
    ipcRenderer.on('menu', (_e, m) => fn(m));
  },

  /** 外部（Claude Code）が project.json を書き換えたときに呼ばれる。 */
  onProjectChanged: (fn) => {
    ipcRenderer.on('project-changed', (_e, info) => fn(info));
    araListeners['project-changed'].push(fn);
  },

  /** 外部（Claude Code）が session.json（トラック）を書き換えたときに呼ばれる。 */
  onSessionChanged: (fn) => {
    ipcRenderer.on('session-changed', (_e, info) => fn(info));
    araListeners['session-changed'].push(fn);
  },

  ...araTest,
});
