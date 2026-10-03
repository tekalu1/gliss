// プラグイン（VST3 + ARA 2。DAW のエディタ欄の WebView2）のモード。
//
// `window.api.mode === 'ara'` のときだけ有効（ara-bridge.js が window.api を作る。Electron では undefined）。
// 他のファイルはここの `ARA` を import して、数か所だけ分岐する（main.js・commands.js・audio.js・tracks.js・menus.js・session.js）。
//
// この文書は **state.js 以外を import しない**（import の輪を作らない。commands.js などが `ARA` を最上位で使える）。
// 画面の部品（描画・トラックの切り替え・解析）は `araBoot(host, b)` で main.js から渡してもらう。
//
// 持つもの:
//  - DAW の選択に画面が追従する（`selection`: 選ばれたリージョンのトラックに切り替え、表示範囲をリージョンに寄せる）
//  - DAW の再生位置・ループを画面に出す（`playhead` → S.head・S.playing・S.loop）／画面の操作を DAW へ（transport）
//  - リージョンの枠・キャッシュの状態（reading・syncing…）・エンジンの状態を、トラックビューとツールバーの札に出す
import { S } from './state.js';

export const ARA = typeof window !== 'undefined' && window.api?.mode === 'ara';
if (ARA) document.documentElement.dataset.mode = 'ara';

const A = {
  transport: true,          // ホストが ARA の再生の制御を持つ（bootstrap.hostCanTransport）。持たなければ再生ボタンを隠す
  fileGuide: false,         // ファイル > ガイドを開く…（C++ が ara_add_file_guide に言い換える）を出す
  compare: false,
  regions: new Map(),       // トラック id → [{ id, song_start, song_end, mod_start, mod_end }]（ソングの秒・修飾＝ソースの秒）
  cache: new Map(),         // トラック id → { state, progress, error }
  engine: { state: 'ready', error: null },
  sel: null,                // 最後の選択（{ track_id, ara_id, region }）
  sig: '',                  // 画面に出したトラックの位置・ガイドの署名（DAW が動かしたら開き直す）
  flashUntil: 0,
  loopHold: 0,              // 画面でループを動かした直後は DAW の値で上書きしない（ミリ秒の時刻）
  chipKey: '',
};
let H = {};                 // main.js から渡される画面の部品（araBoot）

// ---------------------------------------------------------------- 公開（他のファイルが使う）
export const araFeatures = () => ({ transport: A.transport, fileGuide: A.fileGuide, compare: A.compare });
export const araEngine = () => ({ ...A.engine });
export const araCacheOf = (id) => (ARA ? A.cache.get(id) || null : null);
export const araRegions = (t) => (ARA ? A.regions.get(t.id) || null : null);
/** トラックビュー・見出しを描き直すかの判定に足す（リージョンとキャッシュの状態）。 */
export const araSig = () => (ARA ? JSON.stringify([[...A.regions], [...A.cache].map(([k, v]) => [k, v.state])]) : '');

/** ソングの秒 tl（どれかのリージョンの中）を、トラックの「代表の位置」のタイムラインに置き直す
 * （波形・soundRegion は代表の位置を基準にしている。複製したリージョンの上でクリックしても同じ編集の秒になる）。 */
export function araToRep(t, tl) {
  const rs = ARA ? A.regions.get(t.id) : null;
  if (!rs || !rs.length) return tl;
  const r = rs.find((x) => tl >= x.song_start && tl <= x.song_end);
  if (!r) return tl;
  return (t.offset_sec || 0) + (tl - (r.song_start - r.mod_start));
}

/** タイムラインの範囲 [頭, 終わり] を、リージョンの範囲まで広げる（複製したリージョンが代表の位置より後ろにあるとき）。 */
export function araExtent(tl) {
  if (!ARA) return tl;
  let [a, b] = tl;
  for (const rs of A.regions.values()) for (const r of rs) { a = Math.min(a, r.song_start); b = Math.max(b, r.song_end); }
  return [a, b];
}

/** 画面でループを動かしている（DAW の値で上書きしない時間を延ばす）。 */
export function araLoopHold(ms = 800) { A.loopHold = performance.now() + ms; }

/** DAW の再生の制御へ。ホストが制御を持たなければ { ok: false, reason: 'no-controller' }（再生ボタンを隠す）。 */
export async function araTransport(op, arg) {
  if (!ARA) return { ok: false, reason: 'not-ara' };
  if (!A.transport) return { ok: false, reason: 'no-controller' };
  const r = await window.api.transport(op, arg);
  if (r && r.ok === false && r.reason === 'no-controller') {
    A.transport = false;
    document.documentElement.dataset.transport = 'none';
    H.status?.('この DAW は再生の操作を受け付けない。DAW の再生を使う');
  }
  return r;
}
/** 編集の秒 { track_id, sec } か、ソングの秒 { song_sec } へ再生位置を移す。 */
export const araSeek = (arg) => araTransport('seek', arg);
/** ループ。{ a, b, track_id? }（track_id があれば編集の秒、無ければソングの秒）か null（解除）。 */
export function araLoop(arg) { araLoopHold(); return araTransport('loop', arg); }

/** つかんだノートのプレビュー音（EditorRenderer）。 */
export async function araPreview(op, arg) {
  if (!ARA) return { ok: false };
  return window.api.preview(op, arg);
}

/** 原音と比べる（Melodyne の比較。キャッシュを読まずに原音を返す）。 */
export async function araCompare(on) {
  A.compare = !!on;
  H.status?.(`原音と比べる: ${A.compare ? 'オン（編集を鳴らさない）' : 'オフ'}`);
  await window.api.setCompare(A.compare);
  H.syncMenu?.();
}

// ---------------------------------------------------------------- DAW の再生位置
function onPlayhead(p) {
  if (!p || typeof p !== 'object') return;
  const cur = S.session?.current;
  const m = cur && p.mapped ? p.mapped[cur] : null;
  S.head = m != null ? S.off + m : (Number.isFinite(p.song_sec) ? p.song_sec : S.head);
  const playing = !!p.playing;
  const was = S.playing;
  S.playing = playing;
  const loop = Array.isArray(p.loop) && p.loop.length === 2 ? [p.loop[0], p.loop[1]] : null;
  const loopChanged = A.transport && performance.now() > A.loopHold
    && JSON.stringify(loop) !== JSON.stringify(S.loop);
  if (loopChanged) { S.loop = loop; H.render?.(); }
  H.follow?.();
  H.movePlayhead?.();
  if (was !== playing) {
    H.renderToolbar?.();
    // 再生／停止のアイコン。draw.js の renderToolbar は svg の `.hidden` プロパティを触るが、SVG 要素には hidden が無く属性が変わらない
    document.querySelector('#icPlay')?.toggleAttribute('hidden', playing);
    document.querySelector('#icStop')?.toggleAttribute('hidden', !playing);
  }
}

// ---------------------------------------------------------------- DAW の選択への追従
let selPending = null;
let selWaiting = false;
const isVocal = (id) => S.tracks.some((t) => t.id === id && t.kind === 'vocal');

/** DAW で選ばれたリージョンのトラックに切り替える。ドラッグ中・開いている途中・編集の確定中は最後の 1 件だけ覚えて、静かになってから当てる。 */
async function onSelection(sel) {
  A.sel = sel && sel.track_id ? sel : null;
  if (!A.sel) return;
  selPending = A.sel;
  if (selWaiting) return;
  selWaiting = true;
  try {
    while (selPending) {
      await H.waitFor(() => H.idle() && !S.drag && !S.opening && !H.isDragging());
      const s = selPending;
      selPending = null;
      await applySelection(s);
    }
  } finally { selWaiting = false; }
}

async function applySelection(s) {
  let t = S.tracks.find((x) => x.id === s.track_id);
  if (!t) { try { await H.loadSession(); } catch { return; } t = S.tracks.find((x) => x.id === s.track_id); }
  if (!t || t.kind !== 'vocal') return;
  const r = s.region;
  const off = t.offset_sec || 0;
  const whole = !!r && !!t.duration_sec && (r.mod_end - r.mod_start) >= t.duration_sec * 0.9;
  const same = s.track_id === S.session?.current && !!S.vd;
  if (same && whole) return;                              // 全体を選んだだけ: 利用者のズームを動かさない
  const view = r && r.mod_end > r.mod_start ? [off + r.mod_start, off + r.mod_end] : null;
  const ok = await H.selectTrack(s.track_id, { view, first: whole });
  if (ok) A.sig = sigOf();
}

// ---------------------------------------------------------------- ホストの状態の引き直し
let pulling = null;
/** C++ の今の状態（選択・再生位置・リージョン・キャッシュ・エンジン）を引き直す。イベントを取りこぼした後（画面が見えた・開いた直後）に呼ぶ。 */
export function pullHostState() {
  if (!ARA) return Promise.resolve();
  if (!pulling) {
    pulling = (async () => {
      try {
        const h = await window.api.hostState();
        A.regions = new Map();
        A.cache = new Map();
        for (const t of h.tracks || []) {
          if (Array.isArray(t.regions)) A.regions.set(t.track_id, t.regions);
          if (t.cache) A.cache.set(t.track_id, { ...t.cache });
        }
        if (h.engine) A.engine = { state: h.engine.state || 'ready', error: h.engine.error || null };
        if (h.playhead) onPlayhead(h.playhead);
        H.renderTracks?.();
        updateChip();
        if (h.selection && h.selection.track_id && JSON.stringify(h.selection) !== JSON.stringify(A.sel)) onSelection(h.selection);
      } catch (err) {
        console.error('[ara] hostState:', err);
      } finally { pulling = null; }
    })();
  }
  return pulling;
}

function onCache(c) {
  if (!c || !c.track_id) return;
  const prev = A.cache.get(c.track_id);
  A.cache.set(c.track_id, { state: c.state, progress: c.progress ?? null, error: c.error || null });
  if (c.track_id === S.session?.current && c.state === 'ready' && prev && prev.state !== 'ready') flash();
  H.renderTracks?.();
  updateChip();
}

function onEngine(e) {
  if (!e) return;
  A.engine = { state: e.state || 'ready', error: e.error || null };
  if (A.engine.state === 'failed') H.status?.(`エンジンにつながらない。編集は残っている${A.engine.error ? `（${A.engine.error}）` : ''}`);
  updateChip();
}

// ---------------------------------------------------------------- ツールバーの札・中央の箱（モック 03）
const SVG = {
  ring: (p) => `<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="#3a3a3e" stroke-width="2"/><circle cx="8" cy="8" r="6" fill="none" stroke="#d6d6d6" stroke-width="2" stroke-linecap="round" stroke-dasharray="${(Math.max(0.04, p) * 37.7).toFixed(1)} 40" transform="rotate(-90 8 8)"/></svg>`,
  ok: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M5 8.2 7.1 10.3 11 6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  off: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 8h3.5M10.5 8H14M5.5 5.5v5h2.2a2.5 2.5 0 0 0 0-5zM10.5 5.5v5H8.6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M3 13 13 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  err: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2 14.5 13.5h-13z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 6.5v3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="11.6" r=".9" fill="currentColor"/></svg>',
};

function flash() {
  A.flashUntil = performance.now() + 1500;
  updateChip();
  setTimeout(updateChip, 1600);
}

/** 今のトラックの状態 → 札の中身（無ければ null）。色だけでなく形（輪・チェック・切れた線・三角）と文字で区別する。 */
function chipSpec() {
  const cur = S.session?.current;
  const c = cur ? A.cache.get(cur) : null;
  const restart = { label: 'つなぎ直す', run: async () => { await window.api.restartEngine(); pullHostState(); } };
  const retry = { label: 'やり直す', run: () => H.reopen?.() };
  if (A.engine.state === 'failed') {
    return { kind: 'off', icon: SVG.off, title: 'エンジンにつながらない', detail: '編集は残っている', action: restart,
      box: { text: '再生は最後に作った音のまま続く。今の編集は DAW のソングに残っている。新しい編集は、つなぎ直してから。' + (A.engine.error ? `（${A.engine.error}）` : '') } };
  }
  if (c && c.state === 'failed') {
    return { kind: 'err', icon: SVG.err, title: '解析できなかった', detail: '', action: retry,
      box: { text: `このイベントは原音のまま鳴らす。${c.error ? `（${c.error}）` : ''}` } };
  }
  if (c && c.state === 'mismatch') {
    return { kind: 'off', icon: SVG.off, title: 'DAW の音が変わった', detail: '編集を当てていない', action: null, box: null };
  }
  if (A.engine.state === 'starting') {
    return { kind: 'an', icon: SVG.ring(0.1), title: 'エンジンを起動している', detail: '原音を再生中', action: null, box: null };
  }
  if (c && c.state === 'reading') {
    const p = Number.isFinite(c.progress) ? c.progress : 0;
    return { kind: 'an', icon: SVG.ring(p), title: 'ソースを読み込んでいる', percent: Math.round(p * 100), detail: '原音を再生中', action: null, box: null };
  }
  const pop = busyPopup();
  if (pop) {
    const pct = /^\d+%$/.test(pop.percent) ? parseInt(pop.percent, 10) : null;
    return { kind: 'an', icon: SVG.ring((pct || 0) / 100), title: '解析中', percent: pct,
      detail: `${pop.stage ? `${pop.stage.replace(/ · \d+ 秒$/, '')} · ` : ''}原音を再生中`, action: null, box: null };
  }
  const prep = cur ? H.prepOf?.(cur) : null;
  if (prep && (prep.state === 'preparing' || prep.state === 'queued') && prep.stage !== 'view') {
    const p = Math.max(0, Math.min(1, prep.progress || 0));
    return { kind: 'an', icon: SVG.ring(p), title: '解析中', percent: Math.round(p * 100),
      detail: `${prep.stage_label ? `${prep.stage_label} · ` : ''}原音を再生中`, action: null, box: null };
  }
  if (c && c.state === 'syncing') return { kind: 'an', icon: SVG.ring(0.5), title: '編集を反映している', detail: '', action: null, box: null };
  if (c && c.state === 'waiting') return { kind: 'an', icon: SVG.ring(0.1), title: '解析を待っている', detail: '原音を再生中', action: null, box: null };
  if (performance.now() < A.flashUntil) return { kind: 'ok', icon: SVG.ok, title: '準備完了', detail: '', action: null, box: null };
  return null;
}

/** 解析中などの「止める処理」のポップアップ（busy.js。ポップアップ自体は CSS で隠し、中身だけ札に出す）。終わった後の残りは除く。 */
function busyPopup() {
  const pop = H.busyState?.().popup;
  return pop && !pop.done ? pop : null;
}

function updateChip() {
  const slot = document.querySelector('#araChip');
  const box = document.querySelector('#araBox');
  if (!slot) return;
  // 解析が終わった（札が出ていた止める処理が終わった）: 「準備完了」を 1.5 秒だけ出す
  const busy = !!busyPopup();
  if (A.wasBusy && !busy) { A.flashUntil = performance.now() + 1500; setTimeout(updateChip, 1600); }
  A.wasBusy = busy;
  const s = chipSpec();
  const key = s ? JSON.stringify([s.kind, s.title, s.percent ?? null, s.detail, s.action?.label || '', s.box?.text || '']) : '';
  if (key === A.chipKey) return;
  A.chipKey = key;
  slot.textContent = '';
  if (box) { box.textContent = ''; box.hidden = true; }
  if (!s) return;
  const chip = document.createElement('span');
  chip.className = `chip ${s.kind}`;
  chip.setAttribute('role', s.kind === 'err' ? 'alert' : 'status');
  chip.innerHTML = s.icon;
  const b = document.createElement('b');
  b.textContent = s.title;
  chip.append(b);
  if (s.percent != null) {
    const pct = document.createElement('span');
    pct.className = 'pc';
    pct.textContent = `${s.percent}%`;
    chip.append(pct);
  }
  if (s.detail) {
    const d = document.createElement('span');
    d.className = 'd';
    d.textContent = `· ${s.detail}`;
    chip.append(d);
  }
  if (s.action) chip.append(actionButton(s.action));
  slot.append(chip);
  if (box && s.box) {
    const card = document.createElement('div');
    card.className = 'bx';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-label', s.title);
    const t = document.createElement('div');
    t.className = `t ${s.kind}`;
    t.innerHTML = s.icon;
    const tt = document.createElement('span');
    tt.textContent = s.title;
    t.append(tt);
    const m = document.createElement('div');
    m.className = 'm';
    m.textContent = s.box.text;
    const a = document.createElement('div');
    a.className = 'a';
    if (s.action) { const ab = actionButton(s.action); ab.className = 'pri'; a.append(ab); }
    card.append(t, m, a);
    box.append(card);
    box.hidden = false;
  }
}

function actionButton(action) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = action.label;
  b.addEventListener('click', () => { Promise.resolve(action.run()).catch((err) => H.status?.(`${action.label}に失敗: ${err.message}`)); });
  return b;
}

// ---------------------------------------------------------------- 起動・セッションの変化
const norm = (p) => String(p || '').replace(/\//g, '\\').toLowerCase();
function sigOf() {
  const t = S.tracks.find((x) => x.id === S.session?.current);
  return t ? `${t.id}|${t.offset_sec}|${t.duration_sec}|${t.project_dir}|${S.session?.guide || ''}` : '';
}

/** 画面のトラックの一覧が変わった（session-changed の後。main.js が呼ぶ）: 開いていなければ開く・位置が動いたら開き直す。 */
export async function araAfterSession() {
  if (!ARA) return;
  await pullHostState();
  const vocals = S.tracks.filter((t) => t.kind === 'vocal');
  if (!vocals.length) {
    H.clearEmpty?.();
    H.status?.('Gliss を割り当てたイベントがない（DAW でイベントを選んで Gliss を割り当てる）');
    A.sig = '';
    return;
  }
  const cur = S.tracks.find((t) => t.id === S.session?.current && t.kind === 'vocal');
  const shown = !!S.vd && !!S.projectDir && S.tracks.some((t) => norm(t.project_dir) === norm(S.projectDir));
  if (!cur || !shown) {
    const want = A.sel && isVocal(A.sel.track_id) ? A.sel.track_id : (cur || vocals[0]).id;
    if (await H.selectTrack(want, { first: true })) A.sig = sigOf();
    return;
  }
  const sig = sigOf();
  if (sig !== A.sig) {
    A.sig = sig;
    await H.reopen?.();
  }
}

/** 起動（main.js の boot の最後）。host: 画面の部品、b: bootstrap の返り値。 */
export async function araBoot(host, b) {
  H = host;
  A.transport = b.hostCanTransport !== false;
  A.fileGuide = b.fileGuide === true;
  A.compare = !!b.compare;
  A.sel = b.selection && b.selection.track_id ? b.selection : null;
  A.engine = b.engineError ? { state: 'failed', error: b.engineError } : { state: b.engineReady === false ? 'starting' : 'ready', error: null };
  document.documentElement.dataset.transport = A.transport ? 'on' : 'none';
  window.api.onPlayhead(onPlayhead);
  window.api.onSelection(onSelection);
  window.api.onCacheState(onCache);
  window.api.onEngineState(onEngine);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) pullHostState(); });
  setInterval(updateChip, 250);
  updateChip();
  if (b.engineError) {
    H.status('エンジンに接続できていない。つなぎ直すか、ログを確認すること');
    return;
  }
  await pullHostState();
  try {
    await H.loadSession();
  } catch (err) {
    // セッションが無い間（ara_open の前・最初のソースの読み込み中）は待つ。session-changed で araAfterSession が開く
    if (!/トラックが無い/.test(err.message)) { H.status(`トラックを読めない: ${err.message}`); return; }
  }
  if (!S.tracks.some((t) => t.kind === 'vocal')) {
    H.status('イベントを読み込んでいる…（DAW でイベントに Gliss を割り当てるとここに出る）');
    return;
  }
  const want = A.sel && isVocal(A.sel.track_id) ? A.sel.track_id
    : (S.tracks.find((t) => t.id === S.session?.current && t.kind === 'vocal') || S.tracks.find((t) => t.kind === 'vocal')).id;
  const r = A.sel && A.sel.track_id === want ? A.sel.region : null;
  const t = S.tracks.find((x) => x.id === want);
  const view = r && r.mod_end > r.mod_start && t ? [(t.offset_sec || 0) + r.mod_start, (t.offset_sec || 0) + r.mod_end] : null;
  const whole = !r || !t?.duration_sec || (r.mod_end - r.mod_start) >= t.duration_sec * 0.9;
  if (await H.selectTrack(want, { view: whole ? null : view, first: whole })) A.sig = sigOf();
}
