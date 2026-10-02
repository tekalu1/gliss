// AI とつなぐ（ヘルプ > AI とつなぐ…）。見た目の手本は mcp-onboarding-v2 の「② AI とつなぐ」。
//
//  - Claude Code [追加]: main が `claude mcp add-json gliss … --scope user` を実行する。済んだら「追加済み」。
//    claude コマンドが無ければボタンを無効にして、理由はツールチップ。登録済みなら最初から「追加済み」
//  - Claude Desktop [追加]: claude_desktop_config.json の mcpServers に gliss を足す。済んだら「Claude Desktop を再起動」。
//    入っていなければ行を無効に
//  - その他 [設定をコピー]: mcpServers の JSON 断片をクリップボードへ
//  - AI に許可: 編集（既定オン）・保存・書き出し（既定オフ）。bridge.json に書き、AI 側のエンジンがツールのたびに読む
// 接続の状態は出さない（ヘッダーのボタン・「接続中」・AI が操作中の線は作らない）。文での説明も置かない。
import { status } from './engine.js';

const $ = (s) => document.querySelector(s);
let dlg = null;
let bg = null;
let root = null;
let back = null;
let seq = 0;                   // 開き直したら前の状態の読み込みの結果は捨てる

export function aiOpen() { return !!dlg && !dlg.hidden; }

function row(id) { return dlg.querySelector(`[data-cl="${id}"]`); }

/** 行の表示: text（右の短い状態）と、ボタンを出すか・押せるか・ツールチップ。 */
function setRow(id, { text = '', err = false, button = true, enabled = true, title = '', off = false } = {}) {
  const r = row(id);
  const st = r.querySelector('.st');
  st.textContent = text;
  st.classList.toggle('err', !!err);
  st.title = err ? title : '';
  r.classList.toggle('off', !!off);
  const b = r.querySelector('button');
  b.hidden = !button;
  b.disabled = !enabled;
  b.title = err ? '' : title;
  // 無効のボタンはマウスが乗ってもツールチップが出ないので、行にも付ける
  r.title = !enabled && title && !err ? title : '';
}

async function refresh() {
  const my = ++seq;
  setRow('cc', { enabled: false });
  setRow('cd', { enabled: false });
  let s;
  try {
    s = await window.api.ai.status();
  } catch (e) {
    status(`AI とつなぐ: 状態を読めない: ${e.message}`);
    return;
  }
  if (my !== seq) return;
  const cc = s.claudeCode;
  if (cc.registered) setRow('cc', { text: '追加済み', button: false });
  else if (!cc.available) setRow('cc', { enabled: false, title: cc.reason || 'claude コマンドが見つからない', off: true });
  else setRow('cc', {});
  const cd = s.desktop;
  if (!cd.installed) setRow('cd', { enabled: false, title: 'Claude Desktop が見つからない', off: true });
  else if (cd.registered) setRow('cd', { text: '追加済み', button: false });
  else setRow('cd', {});
  for (const k of ['edit', 'save']) dlg.querySelector(`[data-allow="${k}"]`).checked = !!s.allow[k];
}

export function openAi() {
  if (aiOpen()) { dlg.focus(); return; }
  back = document.activeElement;
  document.querySelector('#menu')?.setAttribute('hidden', '');
  setRow('ot', {});
  dlg.hidden = false;
  bg.hidden = false;
  dlg.focus({ preventScroll: true });
  return refresh();
}

export function closeAi() {
  if (!aiOpen()) return;
  seq += 1;
  dlg.hidden = true;
  bg.hidden = true;
  (back && back.isConnected ? back : root).focus({ preventScroll: true });
}

async function add(id) {
  const b = row(id).querySelector('button');
  b.disabled = true;
  const r = id === 'cc' ? await window.api.ai.addClaudeCode() : await window.api.ai.addDesktop();
  if (r?.ok) {
    setRow(id, { text: id === 'cd' ? 'Claude Desktop を再起動' : '追加済み', button: false });
    return r;
  }
  const msg = r?.error || '追加できなかった';
  setRow(id, { text: '追加できなかった', err: true, title: msg });
  status(`${id === 'cc' ? 'Claude Code' : 'Claude Desktop'} に追加できなかった: ${msg}`);
  return r;
}

async function copyConfig() {
  const b = row('ot').querySelector('button');
  await window.api.ai.copyConfig();
  setRow('ot', { text: 'コピーした' });
  clearTimeout(copyConfig.t);
  copyConfig.t = setTimeout(() => { if (b.isConnected) setRow('ot', {}); }, 1500);
}

export function installAiDialog(rootEl) {
  root = rootEl;
  dlg = $('#ai');
  bg = $('#aiBg');
  dlg.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b || b.disabled) return;
    const k = b.dataset.b;
    if (k === 'close') closeAi();
    else if (k === 'cc' || k === 'cd') add(k);
    else if (k === 'copy') copyConfig();
  });
  dlg.addEventListener('change', async (e) => {
    const k = e.target.dataset?.allow;
    if (!k) return;
    const a = await window.api.ai.setAllow({ [k]: e.target.checked });
    for (const x of ['edit', 'save']) dlg.querySelector(`[data-allow="${x}"]`).checked = !!a[x];
  });
  document.addEventListener('keydown', (e) => {
    if (!aiOpen()) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeAi(); }
  }, true);
  bg.addEventListener('pointerdown', closeAi);
}

// ---------------------------------------------------------------- 短い知らせ
let toastTimer = 0;
/** 下の中央に短く出して消す（「3 ノートをコピー」）。 */
export function toast(text, ms = 2200) {
  const t = $('#toast');
  if (!t) return;
  t.textContent = text;
  t.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('on'), ms);
}
