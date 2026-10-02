// 任意機能のアドオン（漢字の歌詞の読みなど。main の addons.mjs）の行と、「モデルと追加の機能」のダイアログ。
//
//  - 行（.fr-model。見た目と操作は解析モデルの行と同じ）: 初回画面（#firstRun）の解析モデルの下に並ぶ。
//    ダウンロード・取り消し・ライセンス・削除。取得したら画面のエンジンがその場で読み直す（再起動は要らない）
//  - ダイアログ（ヘルプ > モデルと追加の機能…）: 曲を開いている間も同じ行を使えるように、初回画面の行
//    （解析モデル・ライセンス・アドオン）をダイアログへ移して出し、閉じたら戻す
//  - エンジンと合わない（アプリを更新してエンジンの依存が変わった）アドオンは読まれず、「更新が要る」と出して取り直させる
import { status } from './engine.js';
import { refreshModels } from './first-run.js';

const $ = (s) => document.querySelector(s);
let rows = null;
let dlg = null;
let bg = null;
let root = null;
let back = null;
let home = null;            // ダイアログを閉じたら行を戻す場所（#firstRun の中）
let list = [];
let progress = { id: null, phase: 'idle', bytes: 0, total: 0 };
const notes = new Map();    // id -> 直近の操作の結果の短い文（「次に起動したときに削除します」など）
const openLicense = new Set();

export function addonsOpen() { return !!dlg && !dlg.hidden; }
export function addonsState() {
  return { open: addonsOpen(), addons: list.map((a) => ({ ...a, note: notes.get(a.id) || null })), progress };
}

function mb(n) { return `${Math.ceil(n / 1e6)} MB`; }
const active = (id) => progress.id === id && ['downloading', 'extracting'].includes(progress.phase);

/** 状態の短い文と、出すボタン。 */
function describe(a) {
  const size = a.size ? `約 ${mb(a.size)}` : '';
  if (active(a.id)) {
    return { text: progress.phase === 'extracting' ? '配置中…' : 'ダウンロード中…', buttons: ['cancel'] };
  }
  const failed = progress.id === a.id && progress.phase === 'failed';
  switch (a.state) {
    case 'ready': return { text: notes.get(a.id) || '使えます', buttons: ['license', 'remove'] };
    case 'environment': return { text: '使えます（開発環境の Python）', buttons: [] };
    case 'removing': return { text: 'Gliss を次に起動したときに削除します', buttons: [] };
    case 'incompatible':
      // 理由（どの依存の版が違うか）はツールチップに
      return { text: `更新が必要です · ${size}`, err: true, title: `この版のエンジンと合いません（${a.reason || '版が違う'}）`,
        buttons: ['license', failed ? 'retry' : 'update', 'remove'] };
    case 'missing': return { text: `任意 · ${size}`, buttons: ['license', failed ? 'retry' : 'download'] };
    case 'unknown': return { text: 'エンジンにつながっていません', buttons: [] };
    default: return { text: a.managed ? 'この版には含まれていません' : '開発版では venv に入れて使います', buttons: [] };
  }
}

const LABEL = { license: 'ライセンス', download: 'ダウンロード', update: '更新', retry: '再試行', cancel: '取り消し', remove: '削除' };

function rowNode(a) {
  const row = document.createElement('div');
  row.className = 'fr-model fr-addon';
  row.dataset.addon = a.id;
  const name = document.createElement('strong');
  name.textContent = a.title;
  name.title = a.description || '';
  const summary = document.createElement('span');
  summary.className = 'fr-muted';
  summary.dataset.r = 'summary';
  const spacer = document.createElement('div');
  spacer.className = 'fr-spacer';
  const bar = document.createElement('div');
  bar.className = 'fr-progress';
  bar.dataset.r = 'progress';
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-label', `${a.title}のダウンロード`);
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', '100');
  bar.append(document.createElement('span'));
  const percent = document.createElement('span');
  percent.className = 'fr-muted';
  percent.dataset.r = 'percent';
  const actions = document.createElement('div');
  actions.className = 'fr-actions';
  for (const b of ['license', 'download', 'update', 'retry', 'cancel', 'remove']) {
    const btn = document.createElement('button');
    btn.dataset.b = b;
    btn.textContent = LABEL[b];
    btn.className = ['download', 'update', 'retry'].includes(b) ? 'fr-primary' : 'fr-quiet';
    btn.hidden = true;
    actions.append(btn);
  }
  const error = document.createElement('span');
  error.className = 'fr-error';
  error.dataset.r = 'error';
  error.setAttribute('role', 'alert');
  row.append(name, summary, spacer, bar, percent, actions, error);
  const lic = document.createElement('div');
  lic.className = 'fr-license';
  lic.dataset.license = a.id;
  lic.hidden = true;
  return [row, lic];
}

function licenseNodes(a) {
  if (!a.licenses.length) {
    const d = document.createElement('div');
    d.textContent = 'ライセンスの一覧はこの版に含まれていません';
    return [d];
  }
  const head = document.createElement('div');
  head.className = 'fr-muted';
  head.textContent = `${a.title}に入るもの（全文はアドオンの LICENSES.txt と THIRD_PARTY_NOTICES.txt）`;
  return [head, ...a.licenses.map((l) => {
    const d = document.createElement('div');
    d.textContent = `${l.name}：${l.license}`;
    return d;
  })];
}

function draw() {
  if (!rows) return;
  const ids = list.map((a) => a.id).join();
  if (rows.dataset.ids !== ids) {
    rows.replaceChildren(...list.flatMap(rowNode));
    rows.dataset.ids = ids;
  }
  for (const a of list) {
    const row = rows.querySelector(`[data-addon="${a.id}"]`);
    const d = describe(a);
    const s = row.querySelector('[data-r="summary"]');
    s.textContent = d.text;
    s.title = d.title || '';
    s.classList.toggle('fr-warn', !!d.err);
    const on = active(a.id);
    const pct = on && progress.total ? Math.min(100, Math.floor(100 * progress.bytes / progress.total)) : 0;
    const bar = row.querySelector('[data-r="progress"]');
    bar.hidden = !on;
    bar.setAttribute('aria-valuenow', String(pct));
    bar.firstElementChild.style.width = `${pct}%`;
    const p = row.querySelector('[data-r="percent"]');
    p.hidden = !on;
    p.textContent = `${pct}%`;
    const busy = !!progress.id && ['downloading', 'extracting'].includes(progress.phase);
    for (const b of row.querySelectorAll('button[data-b]')) {
      b.hidden = !d.buttons.includes(b.dataset.b);
      // ほかのアドオンを取得している間は始めない（一度に 1 つ）
      b.disabled = busy && !on && b.dataset.b !== 'license';
    }
    row.querySelector('[data-r="error"]').textContent = progress.id === a.id && progress.phase === 'failed' ? progress.error : '';
    const lic = rows.querySelector(`[data-license="${a.id}"]`);
    lic.hidden = !openLicense.has(a.id);
    if (!lic.hidden) lic.replaceChildren(...licenseNodes(a));
  }
}

export async function refreshAddons() {
  if (!window.api?.addons) return;
  try {
    const s = await window.api.addons.state();
    list = s.addons || [];
    if (!active(progress.id)) progress = s.progress || progress;
  } catch (e) {
    status(`追加の機能の状態を読めませんでした: ${e.message}`);
  }
  draw();
}

async function act(id, b) {
  if (b === 'license') {
    if (openLicense.has(id)) openLicense.delete(id); else openLicense.add(id);
    draw();
    return;
  }
  try {
    if (b === 'cancel') { await window.api.addons.cancel(); return; }
    if (b === 'remove') {
      const r = await window.api.addons.remove(id);
      if (!r?.removed) return;
      notes.delete(id);
      if (r.addons) list = r.addons;
      status(r.pending ? 'アドオンは使用中のため、Gliss を次に起動したときに削除します' : 'アドオンを削除しました');
      draw();
      return;
    }
    notes.delete(id);
    progress = await window.api.addons.start(id);
    draw();
  } catch (e) {
    progress = { id, phase: 'failed', error: String(e.message || e), bytes: 0, total: 0 };
    draw();
  }
}

/** ダイアログを開く（ヘルプ > モデルと追加の機能…）。初回画面の行をダイアログへ移す。 */
export function openAddons() {
  if (!dlg) return;
  if (!addonsOpen()) {
    back = document.activeElement;
    document.querySelector('#menu')?.setAttribute('hidden', '');
    const body = dlg.querySelector('#addonsBody');
    body.append($('#modelRow'), $('#modelLicense'), rows);
    dlg.hidden = false;
    bg.hidden = false;
  }
  void refreshAddons();
  void refreshModels();
  dlg.focus({ preventScroll: true });
}

export function closeAddons() {
  if (!addonsOpen()) return;
  dlg.hidden = true;
  bg.hidden = true;
  // 行を初回画面（解析モデルの行の位置）へ戻す
  home.before($('#modelRow'), $('#modelLicense'), rows);
  (back && back.isConnected ? back : root).focus({ preventScroll: true });
}

export function installAddons(rootEl) {
  root = rootEl;
  rows = $('#addonRows');
  dlg = $('#addonsDlg');
  bg = $('#addonsBg');
  home = $('#firstRunDrop');
  if (!window.api?.addons || !rows) return;
  rows.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-b]');
    const row = e.target.closest('[data-addon]');
    if (b && row && !b.disabled) void act(row.dataset.addon, b.dataset.b);
  });
  dlg.addEventListener('click', (e) => {
    if (e.target.closest('button')?.dataset.b === 'close') closeAddons();
  });
  document.addEventListener('keydown', (e) => {
    if (!addonsOpen()) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeAddons(); }
  }, true);
  bg.addEventListener('pointerdown', closeAddons);
  window.api.addons.onProgress((next) => {
    progress = next;
    if (next.phase === 'done') {
      notes.set(next.id, '使えます（AI クライアントのエンジンは、つなぎ直すと使えます）');
      status('アドオンを入れました');
    }
    draw();
    if (['done', 'cancelled', 'failed'].includes(next.phase)) void refreshAddons();
  });
  void refreshAddons();
}
