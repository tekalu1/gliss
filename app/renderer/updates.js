// 自動更新の画面（ヘルプ > 更新を確認…）。状態は main（updates.mjs）が持ち、ここは写すだけ。
//
//  - 右下の小さな知らせ（#updNote）: 新しい版がある（自動ダウンロードがオフのとき）・ダウンロード中・
//    「再起動して更新」・更新した後の最初の起動の「<版> に更新しました」。自動の確認の失敗は出さない（ダイアログにだけ出す）
//  - ダイアログ（#upd。見た目は「AI とつなぐ」と同じ）: いまの版と状態・確認／ダウンロード／再起動して更新・
//    ダウンロードの進み具合・この版の内容（releases/<版>.json から作った release-info.json）・
//    設定（自動で確認・自動でダウンロード・ベータ版を受け取る）
//  - 再起動して更新は main が進める（AI 側のエンジンが止まることの確認 → 「保存しますか」→ エンジンを止める → 入れ替え）。
//    「保存しますか」は main から confirm-update で頼まれ、confirmDiscard（main.js）の答えを返す
import { status } from './engine.js';

const $ = (s) => document.querySelector(s);
let dlg = null;
let bg = null;
let note = null;
let root = null;
let back = null;
let st = { phase: 'unavailable' };
let notes = null;           // { version, releases: [{ version, title, date, sections }] }
let showNotes = false;

export function updatesOpen() { return !!dlg && !dlg.hidden; }
export function updatesState() { return { ...st, open: updatesOpen(), notesShown: showNotes, note: note && !note.hidden ? note.textContent : null }; }

/** 状態の短い文と、主のボタン（{ label, act }。null なら出さない）。 */
function describe(s) {
  const t = s.target || '';
  switch (s.phase) {
    case 'unavailable': return { text: 'この版は自動で更新できません', button: null };
    case 'checking': return { text: '確認しています…', button: { label: '確認', act: null } };
    case 'current': return { text: '最新です', button: { label: '確認', act: 'check' } };
    case 'available': return { text: `${t} があります`, button: { label: 'ダウンロード', act: 'download' } };
    case 'downloading': return { text: `${t} をダウンロードしています ${s.progress ?? 0}%`, button: null };
    case 'downloaded': return { text: `${t} の準備ができました`, button: { label: '再起動して更新', act: 'apply' } };
    case 'installing': return { text: '更新しています…', button: { label: '再起動して更新', act: null } };
    case 'error': return { text: s.error || '更新を確認できませんでした', err: true, button: { label: '確認', act: 'check' } };
    default: return { text: '', button: { label: '確認', act: 'check' } };
  }
}

function renderDialog() {
  if (!dlg) return;
  dlg.querySelector('#updVer').textContent = st.version || '';
  const d = describe(st);
  const stEl = dlg.querySelector('#updSt');
  stEl.textContent = d.text;
  stEl.classList.toggle('err', !!d.err);
  stEl.title = d.err ? d.text : '';
  // 準備済みの更新があるときの失敗（再起動して更新が始められなかった）も出す
  if (!d.err && st.error && st.phase === 'downloaded') { stEl.textContent = st.error; stEl.classList.add('err'); }
  const b = dlg.querySelector('#updAct');
  b.hidden = !d.button;
  if (d.button) {
    b.textContent = d.button.label;
    b.dataset.act = d.button.act || '';
    b.disabled = !d.button.act;
  }
  const bar = dlg.querySelector('#updBar');
  bar.hidden = st.phase !== 'downloading';
  bar.setAttribute('aria-valuenow', String(st.progress ?? 0));
  bar.firstElementChild.style.width = `${st.progress ?? 0}%`;
  const off = !st.enabled;
  for (const [k, v] of [['autoCheck', st.autoCheck], ['autoDownload', st.autoDownload], ['beta', st.channel === 'beta']]) {
    const i = dlg.querySelector(`[data-pref="${k}"]`);
    i.checked = !!v;
    // 取得の途中・取得済みはチャネルを変えない（変えると取得した版の扱いがあいまいになる）
    i.disabled = off || (k === 'beta' && ['downloading', 'downloaded', 'installing'].includes(st.phase));
  }
  const cur = notes?.releases?.find((r) => r.version === st.version) || null;
  const link = dlg.querySelector('#updNotesLink');
  link.hidden = !cur;
  link.textContent = showNotes ? 'この版の内容を閉じる' : 'この版の内容';
  const box = dlg.querySelector('#updNotes');
  box.hidden = !(cur && showNotes);
  if (cur && showNotes) {
    box.replaceChildren(...notesNodes(cur));
  }
}

function notesNodes(r) {
  const head = document.createElement('div');
  head.className = 'nh';
  head.textContent = `${r.version} · ${r.date} · ${r.title}`;
  const out = [head];
  for (const s of r.sections) {
    const h = document.createElement('div');
    h.className = 'ns';
    h.textContent = s.title;
    const ul = document.createElement('ul');
    for (const it of s.items) {
      const li = document.createElement('li');
      li.textContent = it;
      ul.append(li);
    }
    out.push(h, ul);
  }
  return out;
}

/** 右下の知らせ。出すものが無ければ隠す。 */
function renderNote() {
  if (!note) return;
  const t = st.target || '';
  let text = '';
  let act = null;
  if (st.phase === 'downloaded') { text = `${t} の準備ができました`; act = ['再起動して更新', 'apply']; }
  else if (st.phase === 'installing') text = '更新しています…';
  else if (st.phase === 'downloading') text = `更新をダウンロードしています ${st.progress ?? 0}%`;
  else if (st.phase === 'available' && !st.autoDownload) { text = `新しい版 ${t} があります`; act = ['ダウンロード', 'download']; }
  else if (st.updatedFrom) { text = `${st.version} に更新しました`; act = ['内容', 'notes']; }
  note.hidden = !text;
  if (!text) return;
  note.querySelector('.t').textContent = text;
  const b = note.querySelector('[data-b="act"]');
  b.hidden = !act;
  if (act) { b.textContent = act[0]; b.dataset.act = act[1]; }
  // 閉じる（×）は「更新しました」にだけ（ほかは操作を待つ知らせなので、ダイアログから扱う）
  note.querySelector('[data-b="dismiss"]').hidden = act?.[1] !== 'notes';
}

function render() {
  renderDialog();
  renderNote();
}

async function run(act) {
  try {
    if (act === 'check') await window.api.updates.check();
    else if (act === 'download') await window.api.updates.download();
    else if (act === 'apply') await window.api.updates.apply();
    else if (act === 'notes') { await window.api.updates.dismissNotice(); openUpdates({ notes: true }); }
  } catch (e) {
    status(`更新: ${e.message}`);
  }
}

/** ダイアログを開く。check: 開いたら確かめる（ヘルプ > 更新を確認…）。notes: この版の内容を開いておく。 */
export function openUpdates({ check = false, notes: withNotes = false } = {}) {
  if (!dlg) return;
  if (!updatesOpen()) {
    back = document.activeElement;
    document.querySelector('#menu')?.setAttribute('hidden', '');
    dlg.hidden = false;
    bg.hidden = false;
  }
  showNotes = withNotes || showNotes;
  render();
  dlg.focus({ preventScroll: true });
  if (check && st.enabled && !['checking', 'downloading', 'downloaded', 'installing'].includes(st.phase)) return run('check');
  return undefined;
}

export function closeUpdates() {
  if (!updatesOpen()) return;
  dlg.hidden = true;
  bg.hidden = true;
  showNotes = false;
  (back && back.isConnected ? back : root).focus({ preventScroll: true });
}

async function setPref(k, v) {
  const value = k === 'beta' ? { channel: v ? 'beta' : 'stable' } : { [k]: v };
  try {
    st = await window.api.updates.setPreferences(value) || st;
  } catch (e) {
    status(`更新の設定: ${e.message}`);
  }
  render();
}

/**
 * @param {HTMLElement} rootEl
 * @param {{ confirmDiscard: () => Promise<boolean> }} o  再起動して更新の前の「保存しますか」
 */
export async function installUpdates(rootEl, { confirmDiscard }) {
  root = rootEl;
  dlg = $('#upd');
  bg = $('#updBg');
  note = $('#updNote');
  if (!window.api?.updates) return;
  dlg.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b || b.disabled) return;
    if (b.dataset.b === 'close') closeUpdates();
    else if (b.id === 'updNotesLink') { showNotes = !showNotes; render(); }
    else if (b.dataset.act) run(b.dataset.act);
  });
  dlg.addEventListener('change', (e) => {
    const k = e.target.dataset?.pref;
    if (k) setPref(k, e.target.checked);
  });
  note.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (b?.dataset.b === 'dismiss') { window.api.updates.dismissNotice(); return; }
    if (b?.dataset.act) { run(b.dataset.act); return; }
    openUpdates({ notes: !!st.updatedFrom });
  });
  document.addEventListener('keydown', (e) => {
    if (!updatesOpen()) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeUpdates(); }
  }, true);
  bg.addEventListener('pointerdown', closeUpdates);
  window.api.updates.onState((s) => { st = { ...st, ...s }; render(); });
  window.api.onConfirmUpdate(async () => {
    closeUpdates();
    return confirmDiscard();
  });
  try {
    const s = await window.api.updates.state();
    notes = s?.notes || null;
    st = { ...s };
    delete st.notes;
  } catch (e) {
    status(`更新の状態を読めませんでした: ${e.message}`);
  }
  render();
}
