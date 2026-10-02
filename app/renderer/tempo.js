// ヘッダーのテンポ表示「120 BPM 4/4」（issue #18。proposal/v3.html §8）とテンポと拍子のポップアップ。
//
// Studio One／Fender Studio Pro のトランスポートと同じ場所・同じ操作:
//   数字をクリックで入力、上下にドラッグ・ホイールで ±1（Shift で ±0.1）。拍子もクリックで入力・ホイールで増減。
//   テンポが無いうちは「— BPM」と薄く出し、そこから入れられる。iXML から読んだ値のときだけ数字の前に小さく「iXML」
//   （ツールチップに出どころ。手で変えると消える）。
// 変更はエンジンの set_tempo（曲の取り消しの履歴に入る）。ドラッグは離したときに 1 回、続けたホイールは
// group で 1 回にまとめる（エンジンが履歴の項目を差し替える）。確定するまではヘッダー・グリッドに見かけの値を出す。
import { S, buttonReleased, clamp } from './state.js';
import { render, renderToolbar } from './draw.js';
import { call, status } from './engine.js';
import { enqueue, handleEngineError, wake } from './edits.js';
import { adoptSession } from './session.js';
import { DEFAULT_TEMPO, bpmText, sigText, tempo } from './grid.js';

const $ = (s) => document.querySelector(s);
const WHEEL_MERGE_MS = 900;       // この間隔より短く続けたホイールは取り消し 1 回
let root = null;
let seq = 0;
let jobs = 0;                     // 当てていないテンポの変更の数（0 になったら見かけの値を外す）

/** ヘッダーの表示を今のテンポに合わせる（draw.js の renderToolbar から）。 */
export function renderTempo() {
  const b = $('#tBpm'); const g = $('#tSig'); const src = $('#tSrc');
  if (!b) return;
  const t = tempo();
  const bt = bpmText(t);
  if (b.textContent !== bt) b.textContent = bt;
  b.classList.toggle('none', !t);
  const st = sigText(t);
  if (g.textContent !== st) g.textContent = st;
  g.classList.toggle('none', !t);
  const ix = !!t && t.source === 'ixml';
  src.hidden = !ix;
  if (ix) {
    const range = t.varies && t.bpm_range ? `。途中でテンポが変わる（${t.bpm_range[0]}〜${t.bpm_range[1]} BPM）: グリッドは頭のテンポで引く` : '';
    src.title = `${t.file || '伴奏'} の iXML（PreSonus のテンポマップ）から読んだ値。手で変えると消える${range}`;
  }
  b.title = t ? 'テンポ: クリックで入力・上下にドラッグかホイールで ±1（Shift で ±0.1）'
    : 'テンポが無い（秒のグリッド）。クリックで入力・ドラッグかホイールで 120 から';
  g.title = '拍子: クリックで入力・ホイールで分子を ±1';
}

function cur() { return { ...DEFAULT_TEMPO, ...(tempo() || {}) }; }
const round2 = (v) => Math.round(v * 100) / 100;

/** set_tempo を順番待ちに入れる。patch は当てる直前に読む（続けたホイールは最後の値で当てる）。 */
function commit(getPatch, { label, group = null } = {}) {
  jobs += 1;
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    jobs -= 1;
    if (!jobs) S.tempoPreview = null;
  };
  return enqueue(async () => {
    const patch = getPatch();
    S.busy = true;
    renderToolbar();
    try {
      const r = await call('set_tempo', { ...patch, group, author: 'human' });
      adoptSession(r.session);
      return r;
    } catch (err) {
      if (!await handleEngineError(err)) status(`テンポを変えられなかった: ${err.message}`);
      return null;
    } finally {
      finish();
      S.busy = false;
      render();
      wake();
    }
  }, { label, cancel: () => { finish(); render(); } });
}

/** 見かけの値を出す（確定するまで。ヘッダー・ルーラー・グリッド）。 */
function preview(patch) {
  S.tempoPreview = { ...cur(), ...patch, source: 'manual' };
  render();
}

// ---------------------------------------------------------------- 入力欄
let editing = null;              // 'bpm' | 'sig'
function openInput(which) {
  const inp = $('#tIn');
  const el = which === 'bpm' ? $('#tBpm') : $('#tSig');
  editing = which;
  const t = tempo();
  inp.value = which === 'bpm' ? (t ? bpmText(t) : '') : sigText(t);
  inp.hidden = false;
  inp.style.left = `${el.offsetLeft - 3}px`;
  inp.style.width = which === 'bpm' ? '4.2em' : '3.6em';
  inp.focus();
  inp.select();
}
function closeInput(ok) {
  const inp = $('#tIn');
  if (!editing) return;
  const w = editing;
  editing = null;
  inp.hidden = true;
  root?.focus({ preventScroll: true });
  if (!ok) return;
  const v = inp.value.trim();
  if (w === 'bpm') {
    const b = parseFloat(v);
    if (!(b >= 20 && b <= 400)) { if (v) status('テンポは 20〜400 の数'); return; }
    if (tempo() && Math.abs(round2(b) - tempo().bpm) < 1e-9) return;
    preview({ bpm: round2(b) });
    commit(() => ({ bpm: round2(b) }), { label: 'テンポ' });
  } else {
    const m = /^(\d+)\s*\/\s*(\d+)$/.exec(v);
    if (!m || +m[1] < 1 || +m[1] > 16 || ![2, 4, 8, 16].includes(+m[2])) {
      status('拍子は 4/4・3/4・6/8 のように（分子 1〜16、分母 2・4・8・16）');
      return;
    }
    const t = tempo();
    if (t && t.num === +m[1] && t.den === +m[2]) return;
    preview({ num: +m[1], den: +m[2] });
    commit(() => ({ numerator: +m[1], denominator: +m[2] }), { label: '拍子' });
  }
}

// ---------------------------------------------------------------- ドラッグ・ホイール
let drag = null;
function onBpmDown(e) {
  if (e.button !== 0 || editing) return;
  e.preventDefault();
  drag = { last: e.clientY, bpm: cur().bpm, moved: false, y0: e.clientY };
  $('#tBpm').setPointerCapture(e.pointerId);
}
function onBpmMove(e) {
  if (!drag) return;
  if (buttonReleased(e)) { onBpmLost(); return; }     // 離したことが届いていない（state.js）
  if (!drag.moved && Math.abs(e.clientY - drag.y0) < 3) return;
  drag.moved = true;
  // 上へ 4 px で +1（Shift で +0.1）。途中で Shift を押しても跳ばないよう、前の位置からの差を積む
  drag.bpm = clamp(drag.bpm - (e.clientY - drag.last) / 4 * (e.shiftKey ? 0.1 : 1), 20, 400);
  drag.last = e.clientY;
  const v = e.shiftKey ? round2(drag.bpm) : Math.round(drag.bpm);
  drag.value = v;
  preview({ bpm: v });
}
function onBpmUp() {
  const d = drag;
  drag = null;
  if (!d) return;
  if (!d.moved) { openInput('bpm'); return; }
  const v = d.value;
  if (v == null || (tempo() && S.session?.tempo && Math.abs(S.session.tempo.bpm - v) < 1e-9)) {
    S.tempoPreview = null;
    render();
    return;
  }
  commit(() => ({ bpm: v }), { label: 'テンポ', group: `tempo-drag-${Date.now()}-${++seq}` });
}

/** 離したこと（pointerup）が届かなかった: 動かしていれば最後に見せていた値で当てる（クリック扱いにはしない）。 */
function onBpmLost() {
  if (!drag) return;
  if (drag.moved) { onBpmUp(); return; }
  drag = null;
}

let wheel = null;                 // { group, last, job: { patch, started }, kind }
function onWheel(e, kind) {
  e.preventDefault();
  e.stopPropagation();
  const d = e.deltaY || e.deltaX;
  if (!d || editing) return;
  const now = Date.now();
  if (!wheel || now - wheel.last > WHEEL_MERGE_MS || wheel.kind !== kind) {
    wheel = { group: `tempo-wheel-${now}-${++seq}`, last: now, job: null, kind };
  }
  wheel.last = now;
  const t = cur();
  let patch; let shown;
  if (kind === 'bpm') {
    const v = clamp(round2(t.bpm - Math.sign(d) * (e.shiftKey ? 0.1 : 1)), 20, 400);
    patch = { bpm: v }; shown = { bpm: v };
  } else {
    const n = clamp(t.num - Math.sign(d), 1, 16);
    patch = { numerator: n, denominator: t.den }; shown = { num: n };
  }
  preview(shown);
  const job = wheel.job;
  if (job && !job.started) { job.patch = patch; return; }     // まだ当てていない: 最後の値で当てる
  const nj = { patch, started: false };
  wheel.job = nj;
  commit(() => { nj.started = true; return nj.patch; },
    { label: kind === 'bpm' ? 'テンポ' : '拍子', group: wheel.group });
}

// ---------------------------------------------------------------- テンポと拍子…（ルーラーの右クリック）
export function openTempoPop(x, y) {
  const pop = $('#popTempo');
  const t = cur();
  $('#tpB').value = String(round2(t.bpm));
  $('#tpN').value = String(t.num);
  $('#tpD').value = String(t.den);
  $('#tpS').value = String(Math.round(t.start_sec * 1000) / 1000);
  pop.hidden = false;
  const r = root.getBoundingClientRect();
  pop.style.left = `${clamp(x, 0, r.width - 270)}px`;
  pop.style.top = `${clamp(y, 0, r.height - 140)}px`;
  $('#tpB').focus();
  $('#tpB').select();
}
export function closeTempoPop() { const p = $('#popTempo'); if (p) p.hidden = true; }
function okTempoPop() {
  const bpm = parseFloat($('#tpB').value);
  const num = +$('#tpN').value; const den = +$('#tpD').value;
  const st = parseFloat($('#tpS').value);
  if (!(bpm >= 20 && bpm <= 400)) { status('テンポは 20〜400 の数'); return; }
  if (!Number.isFinite(st)) { status('1 小節目の位置は秒（負も可）'); return; }
  closeTempoPop();
  root.focus({ preventScroll: true });
  const t = tempo();
  const patch = { bpm: round2(bpm), numerator: num, denominator: den, start_sec: Math.round(st * 1e6) / 1e6 };
  if (t && t.bpm === patch.bpm && t.num === num && t.den === den && Math.abs(t.start_sec - patch.start_sec) < 1e-9
    && t.source === 'manual') return;
  preview({ bpm: patch.bpm, num, den, start_sec: patch.start_sec });
  commit(() => patch, { label: 'テンポと拍子' });
}

export function installTempo(rootEl) {
  root = rootEl;
  const b = $('#tBpm'); const g = $('#tSig'); const inp = $('#tIn');
  b.addEventListener('pointerdown', onBpmDown);
  b.addEventListener('pointermove', onBpmMove);
  b.addEventListener('pointerup', onBpmUp);
  b.addEventListener('pointercancel', () => { drag = null; S.tempoPreview = null; render(); });
  // 押している間に OS のマウスの動きが割り込んでキャプチャが外れた。この後の move は #tBpm に来ないので、
  // 放っておくとドラッグが残り、ボタンを押さずに上を通っただけでテンポが変わる（pointerup の後は drag が無い）
  b.addEventListener('lostpointercapture', onBpmLost);
  b.addEventListener('wheel', (e) => onWheel(e, 'bpm'), { passive: false });
  g.addEventListener('click', () => openInput('sig'));
  g.addEventListener('wheel', (e) => onWheel(e, 'sig'), { passive: false });
  inp.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); closeInput(true); }
    if (e.key === 'Escape') { e.preventDefault(); closeInput(false); }
  });
  inp.addEventListener('blur', () => closeInput(true));
  const pop = $('#popTempo');
  $('#tpOk').addEventListener('click', okTempoPop);
  pop.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); okTempoPop(); }
    if (e.key === 'Escape') { e.preventDefault(); closeTempoPop(); root.focus({ preventScroll: true }); }
  });
  document.addEventListener('pointerdown', (e) => {
    if (!pop.hidden && !pop.contains(e.target) && !$('#menu').contains(e.target)) closeTempoPop();
  }, true);
}

/** 編集 > テンポを入力（コマンドの表）。 */
export function openTempoInput(which = 'bpm') { openInput(which); }

/** テスト用: 入力欄を開いている。 */
export function tempoEditing() { return editing; }
