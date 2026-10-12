// 編集の確定。**ドラッグ中はローカル、離した時点でエンジンのツールを呼ぶ**。
// 呼んだあとは必ず export_view_data で引き直して描き直す（画面は常にエンジンが真）。
//
// **送り方（2026-10-10 承認）**: 離した形はその場で描き（プレビュー）、エンジンには順に送る。
// 次の編集は前の編集の描き直し（export_view_data）を待たずに送り、描き直しは列が空いてから最新 1 回にまとめる。
// 送った編集は、その結果を含む view data を採るまで S.inflight に残し、離した形と「反映待ち」の縞を出し続ける。
// 編集を送っている間に読んだ view data は、その編集を含むか分からないので採らずに読み直す（古い応答は捨てる）。
// 失敗したときだけ知らせる: その操作のプレビューを外し、エンジンの状態を読み直し、後の操作をその上に載せ直す。
//
// **取り消し（issue #16）**: 履歴はエンジンが持つ（セッションの 1 本の履歴。`session.py`）。
// Ctrl+Z / Ctrl+Y はエンジンの undo / redo を呼ぶだけ（トラックの操作も、Claude Code の編集も同じ履歴）。
// 画面が持つのは**まだエンジンに渡していない操作の列**（順番待ち）だけで、Ctrl+Z はまずそこから外す。
import { EngineError, analyzeTake, call, callJob, status, viewData } from './engine.js';
import { beginBusy } from './busy.js';
import { S, adopt, bandOf, fmtTime, histLabel, invalidateWarp, setPlan } from './state.js';
import { render, renderCount, renderToolbar } from './draw.js';
import { invalidate } from './audio.js';
import { adoptSession } from './session.js';
import { ARA, araUpdateChip } from './ara.js';

const AUTHOR = 'human';

let refreshGeneration = 0;
let opActive = false;     // 編集のツール（と取り消し・やり直し）を呼んでいる間
let sentSeq = 0;          // 送り始めた編集の通し番号（読んでいる間に増えたら、その view data は採らない）
let viewDirty = false;    // 当たった編集を、まだ描き直しに採っていない
let refreshing = 0;
export async function refresh({ keepView = true, beforeRender = null } = {}) {
  if (awaiting()) { render(); return; }    // 準備を待つのをやめた: 終わるまで空のまま（描画データを頼むと待たされる）
  refreshing += 1;
  try {
    for (;;) {
      await waitFor(() => !opActive);
      const generation = ++refreshGeneration;
      const trackId = S.session?.current;
      const projectDir = S.projectDir;
      const seq0 = sentSeq;
      const vd = await viewData();
      if (generation !== refreshGeneration || trackId !== S.session?.current || projectDir !== S.projectDir) return null;
      if (seq0 !== sentSeq) continue;     // 読んでいる間に次の編集を送った: どちらの状態か分からない
      // ここまでに送った編集は全部当たり終わっている（読む前に呼び出しが無いのを待った）
      const settled = S.inflight.filter((it) => it.seq != null && it.seq <= seq0 && it.done);
      S.inflight = S.inflight.filter((it) => !settled.includes(it));
      viewDirty = false;
      adopt(vd, { keepView });
      const now = performance.now();
      for (const it of settled) settleItem(it, vd, now);
      restorePreviews();
      beforeRender?.();
      render();
      return vd;
    }
  } finally {
    refreshing -= 1;
    syncMarks();
    wake();
  }
}

/** 送った編集の結果を画面に採った（vd）・失敗した（vd = null）: 後始末と、段階の時間のログ。 */
function settleItem(it, vd, now = performance.now()) {
  for (const fn of it.settles) {
    try { fn(vd); } catch (err) { console.error(err); }
  }
  it.settles.length = 0;
  if (vd && it.op && it.sentAt != null) screenLog(it, now);
}

const sec = (ms) => (Math.max(0, ms) / 1000).toFixed(2).padStart(5);
/** 画面の段階の時間（§7 の書式）。操作の種類と秒だけ（パス・曲名・歌詞は書かない）。 */
function screenLog(it, now) {
  const rel = it.releasedAt ?? it.sentAt;
  const line = `screen op=${it.op} queued=${sec(it.sentAt - rel)} apply=${sec(it.appliedAt - it.sentAt)}`
    + ` refresh=${sec(now - it.appliedAt)} total=${sec(now - rel)}`;
  S.screenTimings.push(line);
  if (S.screenTimings.length > 50) S.screenTimings.shift();
  try { window.api.screenLog?.(line); } catch { /* 書けなくても編集は続ける */ }
}

// 列が空いたら、当たった編集をまとめて 1 回で描き直す（連続した操作の間は読まない）
let kickTimer = null;
function kickRefresh() {
  if (kickTimer || !viewDirty) return;
  kickTimer = setTimeout(async () => {
    kickTimer = null;
    if (!viewDirty || S.queued > 0 || opActive) return;
    try {
      await refresh();
    } catch (err) {
      status(`表示を読み直せなかった: ${err.message}`, { error: true });
    }
  }, 0);
}

// 取り消し・やり直しの結果（編集対象の切り替え・トラックの読み直し）は tracks.js が画面に反映する。
// 解析を待たせたら、その止める処理（busy.js の laterBusy）を返す。描き直してから終わらせる
let historyHandler = async () => null;
export function setHistoryHandler(fn) { historyHandler = fn; }

// ---------------------------------------------------------------- 確定の順番待ち
// **離した操作は捨てない。** 前の編集（確定・Ctrl+Z・書き出しなど）をエンジンが当てている間に
// ドラッグを離したら、前のが終わるのを待ってから同じ量で当てる（DAW と同じく、入力は
// 取りこぼさず順番に処理する）。**すべての編集はここを通る**（issue #16。オリジナルに戻す・歌詞・
// ポップアップの確定・トラックの操作も）。書き出し・開き直しは、ここが空くのを待ってから行う。
const waiters = [];
let pollTimer = null;
const queuedItems = new Set();
let editEpoch = 0;
const CONFLICT_MSG = '別のエンジン（Claude Code など）がこのトラックを書き換えたので、読み直した。今の操作は当てていない';
// 複数の呼び出しでできた操作（複数ノートのピッチ・結合）の途中の呼び出しが競合した: 前の呼び出しの分は当たっている
const CONFLICT_PARTIAL_MSG = '別のエンジン（Claude Code など）がこのトラックを書き換えたので、読み直した。'
  + '今の操作は一部だけ当たった可能性がある。表示を確かめて';
const PREPARING_MSG = '準備中。少し待ってからもう一度';
const PARTIAL_SUFFIX = '（今の操作は一部だけ当たった可能性がある。表示を確かめて）';
// 準備の待ちを「待つのをやめる」でやめた（engine.callJob の abandoned。tracks.js が表示を空にした）
const AWAIT_MSG = '準備中。終わったら表示する（トラックを選ぶと、もう一度待つ）';
const GAVE_UP_MSG = '待つのをやめた。トラックを選ぶと、もう一度解析する';

/** 準備を待つのをやめて、今のトラックの表示を空にしている。 */
export const awaiting = () => !!S.awaitPrep && S.awaitPrep.id === S.session?.current;

/** 競合後は、古い状態で作られた後続の操作とプレビューをすべて破棄する。 */
function discardQueued() {
  editEpoch += 1;
  for (const item of queuedItems) {
    if (item.started || item.canceled) continue;
    item.canceled = true;
    try { item.cancel?.(); } catch { /* 古いプレビューは下でまとめて消す */ }
  }
  S.pending.length = 0;
  // 送った編集の結果は読み直しで採る。競合で捨てた後続の分のピッチの量も消す
  queuedPitch.clear();
  S.drag = null;
  S.stroke = null;
  S.strokesAside = [];
  S.trPreview = null;
  S.local.pitch.clear();
  S.local.btime.clear();
  S.local.fade.clear();
  S.local.mute.clear();
  setPlan(null);
}

/** エンジンの専用エラーを画面の状態と文言に反映する。処理したとき true。
 * partial: 複数の呼び出しでできた操作で、前の呼び出しがもう当たっている（途中で失敗した）。
 * **順番待ちの中から呼ぶ**（競合の読み直しが終わるまで次の操作を始めない）。 */
export async function handleEngineError(err, { partial = false } = {}) {
  if (err.conflict) {
    discardQueued();
    invalidate();
    try {
      adoptSession(await call('list_tracks', {}));
      await refresh();
      status(partial ? CONFLICT_PARTIAL_MSG : CONFLICT_MSG);
    } catch (reloadErr) {
      status(`別のエンジンがこのトラックを書き換えた。読み直せなかった: ${reloadErr.message}`);
    }
    return true;
  }
  if (err.abandoned) {
    status(err.prepContinues ? AWAIT_MSG : GAVE_UP_MSG);
    return true;
  }
  if (err.preparing) {
    S.drag = null;
    S.stroke = null;
    S.strokesAside = [];
    S.local.pitch.clear();
    S.local.btime.clear();
    S.local.fade.clear();
    S.local.mute.clear();
    setPlan(null);
    if (partial) {
      invalidate();
      await refresh().catch(() => {});   // 前の呼び出しの分は当たっている: 今の状態を出す
    } else {
      render();
    }
    status(partial ? `${PREPARING_MSG}${PARTIAL_SUFFIX}` : PREPARING_MSG);
    return true;
  }
  return false;
}

/** 準備を待つのをやめたトラックの編集: エンジンに頼まずに「準備中」で断る（頼むと準備を待たされる）。断ったら true。 */
async function refuseWhileAwaiting() {
  if (!awaiting()) return false;
  await handleEngineError(new EngineError('準備中', { preparing: true }));
  return true;
}

/** 条件が満たされるまで待つ。run の終わりで wake() する。S.busy を外から触る経路
 * （テストなど）でも止まらないよう、待っている間だけ 50 ms ごとにも見る。 */
export function waitFor(cond) {
  if (cond()) return Promise.resolve();
  return new Promise((res) => {
    waiters.push({ cond, res });
    if (!pollTimer) {
      pollTimer = setInterval(() => {
        wake();
        if (!waiters.length) { clearInterval(pollTimer); pollTimer = null; }
      }, 50);
    }
  });
}

/** 待っているもののうち、条件が満たされたものを先に来た順に起こす。 */
export function wake() {
  for (let i = 0; i < waiters.length;) {
    if (waiters[i].cond()) waiters.splice(i, 1)[0].res();
    else i++;
  }
}

let chain = Promise.resolve();
let current = null;       // 列の先頭で動いている操作（run がその操作の送った編集として扱う）

/** 離した操作を順番待ちに入れる。前に入れたものと S.busy（Ctrl+Z など）が終わってから fn を呼ぶ。
 * 止める処理（Ctrl+Z・書き出しなど）を待たされたときは、待っていることをステータスに出す。
 *
 * label（取り消しの名前。「元に戻す: ピッチ」）を渡した操作は、**当たる前なら Ctrl+Z で列から外せる**。
 * cancel: 外したときにプレビューを消す。preview: 描き直し（前の操作の確定）の後にプレビューを載せ直す
 * （当たった結果を画面に採るまで載せ続ける）。settle(vd): 結果を採った（失敗は vd = null）ときの後始末。
 * notes: 反映待ちの縞を付けるノート。op: 段階の時間のログの操作名。 */
const WAIT_MSG = '前の編集を当て終わったら続けて当てる…';
export function enqueue(fn, { label = null, cancel = null, preview = null, settle = null, notes = null,
  op = null, waitMsg = WAIT_MSG } = {}) {
  if (S.busy && !opActive) status(waitMsg);   // 止める処理（Ctrl+Z・保存など）を待つときだけ。編集どうしは黙って続ける
  S.queued += 1;
  const item = { label, cancel, preview, settles: settle ? [settle] : [], notes: notes ? [...notes] : [], op,
    canceled: false, started: false, seq: null, done: false, releasedAt: performance.now() };
  queuedItems.add(item);
  if (label) S.pending.push(item);
  syncMarks();
  const p = chain
    .then(() => waitFor(() => !S.busy))
    .then(() => {
      item.started = true;
      const i = S.pending.indexOf(item);
      if (i >= 0) S.pending.splice(i, 1);
      if (item.canceled) return null;
      S.inflight.push(item);              // 結果を採るまで、離した形のまま（プレビューを載せ続ける）
      current = item;
      sentSeq += 1;                       // run を通らない操作（トラックの操作など）もエンジンを変える
      return fn();
    })
    .finally(() => {
      if (current === item) current = null;
      queuedItems.delete(item);
      S.queued -= 1;
      // 送らずに終わった（動かしていなかった・計画が作れなかった・止める処理）: 待つものは無い
      if (!item.done && S.inflight.includes(item)) {
        S.inflight = S.inflight.filter((it) => it !== item);
        settleItem(item, null);
      }
      if (!S.queued && document.querySelector('#status')?.textContent === WAIT_MSG) status('');
      syncMarks();
      wake();
      if (!S.queued) kickRefresh();
    });
  chain = p.catch(() => {});
  return p;
}

/** 反映待ちのノート（縞）と、ツールバーの「反映中…」。単体は 300 ms を超えたときだけ出す（2026-10-10 承認）。 */
const SYNC_CHIP_MS = 300;
let chipTimer = null;
// 反映待ちに数える操作: 取り消しの名前つき（離した編集）か、編集を送ったもの（Ctrl+Z・保存などの止める処理は数えない）
const live = (it) => !it.canceled && (!!it.label || it.seq != null);
/** 編集を送っている・離した編集が順番を待っている（画面に採っていない）。ARA の札「DAW に反映中…」も見る。 */
export const editsPending = () => S.inflight.some(live) || S.pending.some(live) || viewDirty;
let markRender = null;
export function syncMarks() {
  const ids = new Set();
  for (const it of [...S.inflight, ...S.pending]) {
    if (!live(it)) continue;
    for (const id of it.notes) ids.add(id);
  }
  const changed = ids.size !== S.syncing.size || [...ids].some((id) => !S.syncing.has(id));
  S.syncing = ids;
  araUpdateChip();
  if (changed && markRender == null) {
    // 縞が変わった: 呼び出し側がこの後に描かなければ描く（離した操作は、ふつう同じ流れで描く）
    const at = renderCount();
    markRender = at;
    queueMicrotask(() => { markRender = null; if (renderCount() === at) render(); });
  }
  const chip = document.querySelector('#syncChip');
  if (chip) {
    if (!editsPending()) { clearTimeout(chipTimer); chipTimer = null; chip.hidden = true; }
    else if (chip.hidden && !chipTimer) {
      chipTimer = setTimeout(() => {
        chipTimer = null;
        if (editsPending()) chip.hidden = false;
      }, SYNC_CHIP_MS);
    }
  }
  renderToolbar();
}

/** 順番待ちでまだ当たっていない操作のうち最後のもの（取り消しの名前つき）。 */
export function lastPending() {
  for (let i = S.pending.length - 1; i >= 0; i--) {
    if (!S.pending[i].started && !S.pending[i].canceled) return S.pending[i];
  }
  return null;
}

/** 離したが、まだ画面に採っていないピッチの量（ノートごとの合計、半音）。順番待ち・送った編集の分の和。
 * ピッチのプレビューはこれで描く（shift_pitch は相対なので、続けてドラッグするときの基準にもなる）。 */
export const queuedPitch = new Map();
/** deltas（id → 半音）を queuedPitch に足す（sign = -1 で引く）。 */
export function queuePitch(deltas, sign = 1) {
  for (const [id, v] of deltas) {
    const r = (queuedPitch.get(id) || 0) + sign * v;
    if (Math.abs(r) < 1e-9) queuedPitch.delete(id); else queuedPitch.set(id, r);
  }
}

/** 当たっていない操作のプレビュー（ピッチの量など）を載せ直す。描き直し（adopt）で消えるため。
 * 送った操作（S.inflight）・順番待ち（S.pending）の順に載せる。ピッチは queuedPitch から描き、
 * ピッチのドラッグ中は、そのドラッグの分（S.drag.deltas）も足す。 */
export const draggingPitch = () => S.drag?.type === 'note' && S.drag.axis === 'pitch';
export function restorePreviews() {
  S.local.pitch.clear();
  for (const [id, v] of queuedPitch) S.local.pitch.set(id, v);
  if (draggingPitch()) {
    for (const [id, d] of S.drag.deltas || []) S.local.pitch.set(id, (queuedPitch.get(id) || 0) + d);
  }
  for (const it of S.inflight) if (!it.canceled) it.preview?.();
  for (const it of S.pending) if (!it.canceled) it.preview?.();
}

/** いままでに順番待ちに入れた操作が全部当たり、S.busy も外れ、その結果を画面に採ったら解ける
 * （計画は当たった後の view data の上に描く）。 */
export function afterQueued() {
  return chain.then(() => waitFor(() => !S.busy)).then(async () => {
    if (viewDirty) await refresh().catch(() => {});
  });
}

/** 前の操作が全部当たり終わり、結果を画面に採った（新しい計画を頼んでよい・外部の変更を読み直してよい）。 */
export const idle = () => !S.busy && !S.pendingPlan && S.queued === 0
  && !S.inflight.length && !viewDirty && !refreshing && !opActive;

/** 編集を 1 つ当てる。fn は当たった呼び出しの数を数える関数（done）を受け取る。複数の呼び出しでできた操作は
 * 呼び出しが当たるたびに done() を呼ぶ（途中で失敗したら「一部だけ当たった可能性がある」と伝える）。
 *
 * 既定は**描き直しを待たずに戻る**（列が空いたら最新 1 回にまとめて描き直す）。beforeRender は、
 * この編集の結果を採った描画の直前に呼ぶ（計画のプレビューを同じ描画で外す）。
 * wait: 描き直しまで列の中で待つ（歌詞・方式の切り替え・ポップアップの当て直しなど、結果の上で次を決めるもの）。
 * line: 止める処理の線を出す（方式の切り替え・歌詞。ふつうの編集は出さない）。 */
async function run(fn, label, beforeRender = null,
  { failureBeforeRender = beforeRender, onError = null, onTiming = null, settle = null, wait = false, line = false } = {}) {
  if (opActive || S.busy) return null;
  if (await refuseWhileAwaiting()) return null;
  let item = current;
  if (!item || item.seq != null) {        // 列を通らない呼び出し（鉛筆の再試行など）・1 つの操作の 2 回目
    item = { label, settles: [], notes: [], op: null, canceled: false, started: true, seq: null, done: false,
      releasedAt: performance.now() };
    S.inflight.push(item);
  }
  item.seq = ++sentSeq;
  item.sentAt = performance.now();
  opActive = true;
  S.busy = true;                          // 呼び出している間だけ（入力は止めない。順番は列が守る）
  const busyLine = line ? beginBusy({ label: `${label || '編集'}を反映している…` }) : null;
  syncMarks();
  let applied = 0;
  let r;
  try {
    r = await fn(() => { applied += 1; });
  } catch (err) {
    opActive = false;
    S.busy = false;
    busyLine?.finish();
    onError?.(err);
    // 失敗: この操作のプレビューを外し、エンジンの状態を読み直す（後の操作はその上に載せ直す）
    S.inflight = S.inflight.filter((it) => it !== item);
    settleItem(item, null);
    const partial = applied > 0;
    if (partial) viewDirty = true;
    if (!await handleEngineError(err, { partial })) {
      const vd = await refresh({ beforeRender: failureBeforeRender }).catch(() => null);
      if (!vd && failureBeforeRender) { failureBeforeRender(); render(); }   // 後の読み直しに負けた
      status(`反映できなかった（${label || '編集'}）: ${err.message}${partial ? PARTIAL_SUFFIX : ''}`, { error: true });
    }
    syncMarks();
    wake();
    return null;
  }
  opActive = false;
  S.busy = false;
  item.done = true;
  item.appliedAt = performance.now();
  viewDirty = true;
  invalidate();
  if (settle) item.settles.push((vd) => { if (vd) settle(vd); });
  if (beforeRender) item.settles.push(() => beforeRender());
  if (onTiming) {
    item.settles.push((vd) => {
      if (vd) onTiming({ applyMs: item.appliedAt - item.sentAt, viewMs: performance.now() - item.appliedAt });
    });
  }
  busyLine?.finish();
  syncMarks();
  wake();
  // 列を通らない呼び出し（鉛筆の再試行など）は、列の終わりの描き直しが無い: 列が空ならここで頼む
  if (!S.queued && !wait) kickRefresh();
  if (wait) {
    // 結果の上で次を決める操作: この操作の結果を画面に採るまで列の中で待つ
    try { await refresh(); } catch (err) { status(`表示を読み直せなかった: ${err.message}`, { error: true }); }
    await waitFor(() => !S.inflight.includes(item) || !refreshing);
  }
  if (line) status('');                   // 「音素を切っている…」などの途中の表示を消す
  return r;
}

/** blob の中央を上下 → ピッチ。半音スナップはしない（連続値）。複数ノートは 1 回の呼び出し（取り消し 1 回）。 */
export function applyPitch(deltas, { label = null } = {}) {
  const items = [...deltas.entries()].filter(([, d]) => Math.abs(d) > 1e-4);
  if (!items.length) { restorePreviews(); render(); return Promise.resolve(); }
  return run(async () => {
    const notes = items.map(([id, d]) => ({ note_id: id, cents: d * 100 }));
    await call('shift_pitch', { notes, author: AUTHOR, label }, { busy: false });
  }, label || 'ピッチの編集');
}

/** ピッチの操作（ドラッグ・Q）を順番待ちに入れる。離した量はすぐ queuedPitch に入れて描き、
 * 結果を画面に採った・失敗した・Ctrl+Z で外したときに引く。 */
export function enqueuePitch(deltas, { label = 'ピッチ', applyLabel = null, op = 'pitch' } = {}) {
  const d = new Map([...deltas].filter(([, v]) => Math.abs(v) > 1e-4));
  queuePitch(d);
  let out = false;
  const unqueue = () => { if (!out) { out = true; queuePitch(d, -1); } };
  restorePreviews();
  render();
  return enqueue(() => applyPitch(d, { label: applyLabel }), {
    label, op, notes: [...d.keys()], settle: unqueue, cancel: unqueue,
  });
}

/** 半音に合わせる（Q）: 帯の高さ（ノートの平均の音程）をいちばん近い半音へ（Melodyne と同じ）。
 * 当たるまでは帯と線を動かしておく（ドラッグと同じプレビュー）。取り消し 1 回。 */
export function snapToSemitone(ids) {
  const deltas = new Map();
  for (const id of ids) {
    const n = S.byId.get(id);
    if (!n || !n.pitch_editable) continue;
    const band = bandOf(n);
    const d = Math.round(band) - band;
    if (Math.abs(d) > 1e-3) deltas.set(id, d);
  }
  if (!deltas.size) { status('もう半音に合っている'); return Promise.resolve(); }
  return enqueuePitch(deltas, { label: '半音に合わせる', applyLabel: '半音に合わせる', op: 'q' });
}

let groupSeq = 0;
/** 結合（Ctrl+J）: 接して並ぶノートをまとめて 1 つに（取り消し 1 回）。ids は並び順。 */
export function mergeMany(ids) {
  if (ids.length < 2) return Promise.resolve();
  const group = `merge-${Date.now().toString(36)}-${++groupSeq}`;
  return enqueue(() => run(async (done) => {
    for (let k = 1; k < ids.length; k++) {
      await call('merge_notes', { note_a: ids[0], note_b: ids[k], author: AUTHOR, group }, { busy: false });
      done();
    }
    S.sel = [ids[0]];
  }, 'ノートの結合'), { label: '結合', notes: ids, op: 'merge' });
}

/** 無音にする（Del）／無音を戻す（右クリック）。長さ・位置は変えず、戻すのは無音だけ（ピッチ等の編集は残る）。
 * ids は 1 回の呼び出し = 1 つの changeset（取り消し 1 回）。ミュートツールは、なぞっている間の見かけを
 * S.local.mute に持ち、当たるまでは preview で載せ直す。 */
function applyMute(ids, to) {
  if (!ids.length) return Promise.resolve();
  const label = to ? '無音にする' : '無音を戻す';
  const preview = () => { for (const id of ids) S.local.mute.set(id, to); };
  preview();                            // 離した（押した）形をその場で描く
  render();
  return enqueue(() => run(async () => {
    const r = await call(to ? 'mute_notes' : 'unmute_notes', { note_ids: ids, author: AUTHOR }, { busy: false });
    if (!r.changeset) status(to ? 'もう無音になっている' : '無音のノートが無かった');
  }, label), { label, preview, notes: ids, op: 'mute', cancel: () => { for (const id of ids) S.local.mute.delete(id); } });
}
export const muteNotes = (ids) => applyMute(ids, true);
export const unmuteNotes = (ids) => applyMute(ids, false);

/** フェード（issue #20）: `set_fade`。fadeIn / fadeOut は編集後の秒（省くとそのまま、0 で消す）。**順番待ちの中から呼ぶ**。 */
export function applyFade(ids, { fadeIn = null, fadeOut = null } = {}) {
  return run(async () => {
    const r = await call('set_fade', { note_ids: ids, fade_in_sec: fadeIn, fade_out_sec: fadeOut, author: AUTHOR },
      { busy: false });
    return r;
  }, 'フェード');
}

/** フェードを消す（右クリック）。 */
export function clearFades(ids) {
  if (!ids.length) return Promise.resolve();
  const preview = () => { for (const id of ids) S.local.fade.set(id, { fi: 0, fo: 0 }); };
  preview();
  render();
  return enqueue(() => applyFade(ids, { fadeIn: 0, fadeOut: 0 }), {
    label: 'フェードを消す', preview, notes: ids, op: 'fade', cancel: () => { for (const id of ids) S.local.fade.delete(id); },
  });
}

/** 境目の接続／切り離し（右クリックの「切り離す」「つなぐ」。音は変わらない）。 */
export function setConnection(a, b, connected) {
  return enqueue(() => run(
    () => call('set_connection', { note_a: a, note_b: b, connected, author: AUTHOR }, { busy: false }),
    connected ? 'つなぐ' : '切り離し'), { label: connected ? 'つなぐ' : '切り離し', notes: [a, b], op: 'connection' });
}

/** 隙間のある境目を「つなぐ」: 前のノートの尻を次のノートの頭まで伸ばして接続する
 * （端を隣まで伸ばして吸着させるドラッグと同じ計画・同じ確定）。 */
export function connectGap(a) {
  return enqueue(async () => {
    const data = await requestPlan({ op: 'edge', note_id: a, side: 'end', detach: false });
    if (!data) return;
    if (data.snap_x == null) { status('隣のノートまで伸ばせない'); return; }
    await applyPlan({ planId: data.plan_id, x: data.snap_x, label: 'つなぐ' });
  }, { label: 'つなぐ', notes: [a], op: 'connection' });
}

/** 子音｜母音の境目を元に戻す（その音素の両側の境目の move_boundary を外す）。 */
export function resetBoundaries(ids) {
  if (!ids.length) return Promise.resolve();
  return enqueue(() => run(
    () => call('reset_to_original', { boundary_ids: ids, author: AUTHOR }, { busy: false }), '音素の境目を元に戻す'),
  { label: '音素の境目を元に戻す', op: 'bound' });
}

/** エンジンに計画を作らせて中身（JSON）を読む。
 *
 * 計画 = 節ごとの「編集後の秒 = cur + d × x」。ドラッグ中・スライダーを動かしている間は
 * 画面がこの式で描き、離したら同じ計画を `applyPlan` で確定する。 */
export async function requestPlan(args) {
  const epoch = editEpoch;
  const r = await call('plan_edit', args);
  const data = await window.api.readJson(r.path);
  if (epoch !== editEpoch) return null;
  return data;
}

/** 計画を x（と pitch）で確定する。drag = true なら確定後に計画を外す（ドラッグの終わり）。
 *
 * ポップアップ（ガイドに合わせる）は `replaces` に前回の changeset を渡す。エンジンが
 * それを取り消してから**同じ計画**を当てるので、プレビューと確定が同じ基準になる
 * （取り消しの履歴でも前回の項目と入れ替わる = ポップアップ 1 回 = 取り消し 1 回）。
 * strength = 音程の割合 { center, shape, includeManual, restart }（計画に data.strength があるとき。
 * center / shape = null はノートごとの今の割合のまま。`timing.apply_plan`）。
 * timing = タイミングの割合 0〜1（計画に data.timing_ratio があるとき。選んだノートをこの割合に。x とは一緒に使わない。
 * 2026-10-11 承認）。手で直したノート・合わせ直すノート（includeManual・restart）は計画を作ったときの値を使う。 */
export async function applyPlan({ planId, x = null, pitch = null, replaces = null, drag = true, label = null,
  onTiming = null, strength = null, timing = null }) {
  // ドラッグは結果を待たずに次へ（計画のプレビューは結果を採るまで残す）。ポップアップは当て直しの基準が要るので待つ
  let ok = false;
  const after = () => {
    // ドラッグは成功しても失敗してもプレビューを外す。ただし**この計画のときだけ**
    // （順番待ちの間に「ガイドに合わせる」を開いていたら、その計画は残す）
    if (drag) { if (S.plan?.data.plan_id === planId) setPlan(null); }
    else if (S.plan && ok && S.plan.data.plan_id === planId) {   // 表示中の view data がこの値まで当たっている
      S.plan.x0 = x || 0;                // （スライダーの今の値ではなく、確定した値）
      S.plan.pitch0 = pitch || 0;
      S.plan.str0 = strength;
      if (timing != null) S.plan.t0 = timing;
      invalidateWarp();
    }
  };
  const str = strength && (strength.center != null || strength.shape != null) ? strength : null;
  if (!(Math.abs(x || 0) > 1e-9) && !(Math.abs(pitch || 0) > 1e-9) && !replaces && !str && timing == null) {
    ok = true;
    after();
    render();
    return { ok: true, changeset: null };
  }
  const cs = await run(async () => {
    const r = await call('apply_plan', {
      plan_id: planId, x: timing == null ? x : null, pitch, replaces, author: AUTHOR, label,
      ...(timing != null ? { timing } : {}),
      ...(str ? { pitch_center: str.center, pitch_shape: str.shape, include_manual: !!str.includeManual,
        restart: str.restart?.length ? str.restart : null } : {}),
    }, { busy: false });
    ok = true;
    if (r.snapped) status('接続した');
    else if (r.clamped) status('隣にぶつかる手前で止めた');
    return r.changeset || null;
  }, drag ? 'タイミングの編集' : 'ガイドに合わせる', after, { onTiming, wait: !drag });
  return { ok, changeset: cs };
}

/** 鉛筆で描いた線 → `set_pitch_curve(mode="draw")`。points = [[編集前の秒, MIDI], ...]。
 *
 * 範囲はエンジンが有声のフレームに切り詰める（無声には音程が無いので描いても効かない）。
 * 確定後の view data で描き直すまで、描いた線（S.stroke）のプレビューを残す（ちらつかせない）。 */
/** 描線の段階。描いている次の線があれば、前の線の段階では S.strokePhase（描いている線の段階）を変えない。 */
function strokePhase(stroke, phase) {
  stroke.phase = phase;
  if (S.stroke === stroke || !S.stroke) S.strokePhase = phase;
}
/** 描線を画面から外す（結果を採った・捨てた）。 */
function dropStroke(stroke, phase) {
  S.strokesAside = S.strokesAside.filter((st) => st !== stroke);
  if (S.stroke === stroke) S.stroke = null;
  if (!S.stroke) S.strokePhase = phase;
}
/** 確かめ・再送が要る描線を S.stroke に戻す（描線の操作の札は S.stroke に対して出る）。
 * 次の線を描いている間は戻せないので外し、知らせだけを出す。戻したとき true。 */
function holdStroke(stroke) {
  if (S.stroke === stroke) return true;
  S.strokesAside = S.strokesAside.filter((st) => st !== stroke);
  if (S.stroke) return false;
  S.stroke = stroke;
  return true;
}

export async function applyDraw(points, stroke = S.stroke) {
  if (!stroke || stroke.trackId !== S.session?.current || stroke.projectDir !== S.projectDir) {
    if (stroke) dropStroke(stroke, 'idle');
    render();
    status('編集対象が変わったため描線を送信しませんでした');
    return null;
  }
  const baseline = stroke.baselineIds || new Set((S.vd?.edits || []).map((e) => e.id));
  stroke.baselineIds = baseline;
  strokePhase(stroke, 'pending');
  let failure = null;
  const gone = () => stroke.trackId !== S.session?.current || stroke.projectDir !== S.projectDir;
  // 当たった: 結果を採った描画で描線を外す（それまでは描いた線のまま。待たずに次の編集へ）
  const settled = (vd) => {
    if (gone()) { dropStroke(stroke, 'idle'); return; }
    if (drawObserved(vd, stroke, points)) { dropStroke(stroke, 'committed'); return; }
    if (!holdStroke(stroke)) { status('描いたピッチが表示に見当たらない。表示を確かめてください', { error: true }); return; }
    strokePhase(stroke, 'checking');
    status('編集は受け付けられました。表示を確認中です');
  };
  const r = await run(async () => {
    // 元に戻す線（ペンの右ドラッグ）は points を持たず、範囲だけを渡す（エンジンが録音のピッチを入れる）
    const r = await call('set_pitch_curve', stroke.restore
      ? { mode: 'restore', start_sec: stroke.range[0], end_sec: stroke.range[1], author: AUTHOR }
      : { points, mode: 'draw', author: AUTHOR }, { busy: false });
    return r;
  }, stroke.restore ? 'ピッチを元に戻す' : 'ピッチを描く', null,
  { failureBeforeRender: null, onError: (err) => { failure = err; }, settle: settled });
  if (r) return r;
  // 応答だけが失われた可能性がある。編集 ID と描線の重なる区間を照合してから再試行可能にする。
  strokePhase(stroke, 'checking');
  let vd = null;
  try { vd = await refresh(); } catch { /* 状態を読めない間は再送しない */ }
  if (vd && drawObserved(vd, stroke, points)) {
    dropStroke(stroke, 'committed');
    status('');                          // 応答は失われたが当たっていた: 失敗の知らせを消す
    render();
    return { ok: true, reconciled: true };
  }
  // ツールが明示的に拒否した場合だけ再送できる。通信断など結果不明の呼び出しは
  // 遅れて適用される可能性があるため、表示を読み直しても再送しない。
  const failed = failure instanceof EngineError && !failure.conflict;
  if (!holdStroke(stroke)) {
    // 次の線を描いている: 前の線は外して、知らせだけを出す（描いている線は消さない）
    status(failed ? '前に描いたピッチを確定できませんでした' : '前に描いたピッチの適用状態が不明です', { error: true });
    render();
    return null;
  }
  strokePhase(stroke, failed ? 'failed' : 'checking');
  status(failed ? '手描きピッチを確定できませんでした。描線を保持しています'
    : '適用状態が不明です。描線を保持し、再送を保留しています', { error: true });
  render();
  return null;
}

function drawObserved(vd, stroke, points) {
  if (!vd) return false;
  if (stroke.trackId !== S.session?.current || stroke.projectDir !== S.projectDir) return false;
  const a = points[0][0]; const b = points[points.length - 1][0];
  if (stroke.restore) {
    return (vd.edits || []).some((e) => e.kind === 'pitch_draw' && e.params?.restore && !stroke.baselineIds.has(e.id)
      && (!e.author || e.author === AUTHOR) && e.target?.start_sec <= b + 0.05 && e.target?.end_sec >= a - 0.05);
  }
  const at = (t) => {
    let i = 0;
    while (i + 1 < points.length && points[i + 1][0] < t) i++;
    const [ta, ma] = points[i]; const [tb, mb] = points[Math.min(i + 1, points.length - 1)];
    return tb === ta ? ma : ma + (mb - ma) * (t - ta) / (tb - ta);
  };
  return (vd.edits || []).some((e) => {
    if (e.kind !== 'pitch_draw' || stroke.baselineIds.has(e.id) || e.author && e.author !== AUTHOR) return false;
    const ep = e.params?.points;
    if (!Array.isArray(ep) || ep.length < 2) return false;
    if (e.target?.start_sec < a - 0.002 || e.target?.end_sec > b + 0.002) return false;
    return ep.every(([t, m]) => t >= a - 0.002 && t <= b + 0.002 && Math.abs(at(t) - m) < 0.002);
  });
}

export async function retryDraw(stroke = S.stroke) {
  if (!stroke || !stroke.points || !['failed', 'checking'].includes(stroke.phase)) return null;
  let vd;
  try { vd = await refresh(); } catch { status('適用状態を確認できません。再試行は保留しています'); return null; }
  if (!vd) return null;
  if (drawObserved(vd, stroke, stroke.points)) {
    dropStroke(stroke, 'committed');
    status('手描きピッチは既に確定しています');
    render();
    return { ok: true, reconciled: true };
  }
  if (stroke.phase === 'checking') {
    status('適用状態が不明です。二重適用を避けるため再送を保留しています');
    render();
    return null;
  }
  return applyDraw(stroke.points, stroke);
}

/** はさみ: sec（編集前の秒）でノートを分ける。吸着は画面側で済ませた時刻を渡す。 */
export function splitNote(noteId, sec) {
  return enqueue(() => run(
    () => call('split_note', { sec, note_id: noteId, author: AUTHOR }, { busy: false }), 'ノートの分割'),
  { label: '分割', notes: [noteId], op: 'cut' });
}

/** はさみ: 境目のダブルクリックで結合。 */
export function mergeNotes(a, b) {
  return enqueue(() => run(
    () => call('merge_notes', { note_a: a, note_b: b, author: AUTHOR }, { busy: false }), 'ノートの結合'),
  { label: '結合', notes: [a, b], op: 'merge' });
}

/** つなぎのなだらかさ（ポップアップのスライダーを離したとき）。**順番待ちの中から呼ぶ**。
 *
 * ポップアップの中の当て直しは `replaces` に前回の changeset を渡す（ポップアップ 1 回 = 取り消し 1 回）。 */
export async function applyTransition({ value, noteIds, pair = null, replaces = null }) {
  let ok = false;
  const cs = await run(async () => {
    const [a, b] = pair ? pair.split('|') : [null, null];
    const r = await call('set_transition', {
      value, note_ids: !pair && noteIds && noteIds.length ? noteIds : null, note_a: a, note_b: b,
      replaces, author: AUTHOR,
    });
    ok = true;
    return r.changeset || null;
  }, 'つなぎのなだらかさ', null, { wait: true });
  return { ok, changeset: cs };
}

/** 音素境界を左右 → `move_boundary`。隣り合う 2 音素の長さが同時に変わる。 */
export function applyBoundary(boundaryId, dt) {
  if (Math.abs(dt) < 1e-4) {
    S.local.btime.clear();
    invalidateWarp();
    render();
    return Promise.resolve();
  }
  return run(async () => {
    const r = await call('move_boundary', {
      boundary_id: boundaryId, ms: dt * 1000, author: AUTHOR,
    }, { busy: false });
    if (r.moved?.clamped) status(r.moved.note || '20 ms の下限に当たった');
  }, '音素境界の移動');
}

/** 歌詞を**区間ごと**に入れる → その区間だけアラインし直す → 描き直し。取り消せる（「元に戻す: 歌詞」）。
 *
 * `range` は `{start_sec, end_sec}`（編集前の秒）。省くと素材全体の歌詞になる。
 * 空文字を渡すとその区間の歌詞が消える。 */
export function setLyrics(text, range) {
  const t = (text || '').trim();
  const cur = range?.start_sec == null
    ? (S.lyrics || '')
    : (S.lyricsEntries.find((e) => e.start_sec != null
      && Math.abs(e.start_sec - range.start_sec) < 0.06)?.text || '');
  if (t === cur.trim()) return Promise.resolve();
  return enqueue(() => run(async () => {
    status('歌詞から音素を切っている…');
    // set_lyrics(reanalyze) が音素だけを取り直す（F0 と DTW のキャッシュは捨てない）。
    // そのあとの analyze_take はキャッシュを使うので速い。
    await call('set_lyrics', {
      text: t, source: 'take', reanalyze: true, author: AUTHOR,
      start_sec: range?.start_sec ?? null, end_sec: range?.end_sec ?? null,
    });
    await analyzeTake({ label: '音素を切り直している', target: S.take?.name || '',
      onProgress: (sec) => status(`音素を切っている… ${sec} 秒`) });
  }, '歌詞の設定', null, { wait: true, line: true }), { label: '歌詞' });
}

/** ピッチ検出の方式を替えて、開いているトラックを解析し直す。
 * ARA では現在の修飾だけを変更し、解析を終えてから表示を確定する。 */
export function setF0Estimator(estimator, onChanged) {
  return enqueue(() => run(async () => {
    const result = await call('set_f0_estimator', { estimator, scope: ARA ? 'current' : undefined });
    status('ピッチを解析し直している…');
    await analyzeTake({ label: 'ピッチを解析し直している', target: S.take?.name || '',
      onProgress: (sec) => status(`ピッチを解析し直している… ${sec} 秒`), background: !ARA });
    onChanged?.(result);
    return result;
  }, 'ピッチ検出の方式', null, { wait: true, line: true }), { label: 'ピッチ検出の方式' });
}

/** 歌詞レーンの 1 音節を直す。エンジンの changeset なので Ctrl+Z で戻せる。 */
export function setNoteSyllable(noteId, syllableIndex, kana) {
  return enqueue(() => run(async () => {
    status('音節を直している…');
    await call('set_note_syllable', { note_id: noteId, syllable_index: syllableIndex,
      kana, author: AUTHOR });
  }, '音節の修正', null, { wait: true, line: true }), { label: '音節' });
}

/** 歌詞のテキストファイルをまるごと渡す（ファイル > 歌詞を読み込む）。 */
export function loadLyricsText(content) {
  return enqueue(() => run(async () => {
    status('歌詞を読み込んでいる…');
    const r = await call('set_lyrics', { from_text: content, source: 'take',
      reanalyze: true, author: AUTHOR });
    await analyzeTake({ label: '音素を切り直している', target: S.take?.name || '',
      onProgress: (sec) => status(`音素を切っている… ${sec} 秒`) });
    return r;
  }, '歌詞の読み込み', null, { wait: true, line: true }), { label: '歌詞' });
}

/** 譜面と録音の対応をエンジンに判定させ、確かな区間だけ歌詞として確定する。 */
export function loadLyricsScore(path, track) {
  return enqueue(() => run(async () => {
    status('譜面と発音の位置を照合している…');
    return call('import_lyrics', { path, track, author: AUTHOR, reanalyze: true });
  }, '譜面の読み込み', null, { wait: true, line: true }), { label: '歌詞' });
}

/** WAV を書き出す。**元と同じ長さ・開始位置**で、編集区間だけ差し替わる。
 *
 * 既定（ファイル > 書き出し、Ctrl+E）は**ダイアログを出さず**、元ファイルの隣に `<名前>_ve.wav`
 * （既存のファイルは上書きせず `_ve(2)` …。置き場の決め方はエンジン側 `render/export.py` に 1 つだけ）。
 * `ask: true`（書き出し先を選んで書き出し…、Ctrl+Shift+E）で保存ダイアログ。
 * 元の WAV の BWF（bext の TimeReference＝DAW 上の位置）は書き出しにも入るので、DAW に戻せば元の位置に揃う。
 * **順番待ちの編集が全部当たってから書き出す**（離した直後の編集も書き出しに入る）。 */
export async function exportWav({ ask = false } = {}) {
  if (S.opening) { status('開いている途中。終わってから書き出すこと'); return null; }
  let path = null;
  if (ask) {
    path = await window.api.saveDialog(S.vd?.export_default_path || null);
    if (!path) return null;
  }
  return enqueue(() => exportNow(path), { waitMsg: '前の編集を当ててから書き出します' });
}

async function exportNow(path) {
  S.busy = true;
  const name = String(path || S.vd?.export_default_path || '').split(/[\\/]/).pop();
  const busy = beginBusy({ label: 'WAV に書き出している', modal: true, target: name });
  renderToolbar();
  try {
    status('書き出している…');
    const r = await callJob('export_wav', { ...(path ? { path } : {}), background: true },
      (sec) => status(`書き出している… ${sec} 秒`), { busy, stage: '音を作っている' });
    const n = (r.replaced_spans_sec || []).length;
    // 「DAW 上の位置」は元の WAV に bext があったときだけ（無いクリップの bext は元のファイル内の位置）
    // トラックの位置をずらしていれば、DAW 上の位置（TimeReference）もその量だけ動いている（§4）
    const moved = Math.abs(r.timeline_offset_sec || 0) > 1e-9;
    const pos = (r.bwf?.source_time_reference != null || moved) && r.bwf?.time_reference_sec != null
      ? ` / DAW 上の位置 ${fmtTime(r.bwf.time_reference_sec)}` : '';
    const shift = moved ? ` / 位置 ${r.timeline_offset_sec > 0 ? '+' : '−'}${Math.abs(r.timeline_offset_sec).toFixed(3)} 秒ずらした` : '';
    const posWarn = (r.warnings || []).find((w) => w.includes('DAW 上の位置') || w.includes('手で合わせる'));
    status(`書き出した: ${String(r.path || path).split(/[\\/]/).pop()}`
      + `（${r.duration_sec} 秒 / ${r.sr} Hz / ${r.subtype}${pos}${shift} / 差し替え ${n} 区間`
      + ` ${r.replaced_sec} 秒 / ${r.elapsed_sec} 秒）${posWarn ? ` ${posWarn}` : ''}`);
    return r;
  } catch (err) {
    status(err.cancelled ? '書き出しを取り消した' : `書き出しに失敗: ${err.message}`);
    return null;
  } finally {
    busy.finish();
    S.busy = false;
    renderToolbar();
    wake();
  }
}

export function resetOriginal(ids) {
  if (!ids.length) return Promise.resolve();
  return enqueue(() => run(async () => {
    await call('reset_to_original', { note_ids: ids, author: AUTHOR }, { busy: false });
  }, 'オリジナルに戻す'), { label: 'オリジナルに戻す', notes: ids, op: 'reset' });
}

/** Ctrl+Z: **まだ当たっていない操作があれば列から外すだけ**（画面からすぐ消える。エンジンは呼ばない）。
 * 無ければエンジンの履歴の最後の操作を取り消す（順番待ちに入れる。確定中の操作を追い越さない）。 */
export function undo() {
  const dr = S.edgeDraft?.drag;
  if (dr && !dr.applying && (dr.moved || S.pendingPlan)) {
    dr.canceled = true;
    clearTimeout(dr.planTimer);
    S.edgeDraft = null;
    if (S.drag === dr) S.drag = null;
    if (dr.planId && S.plan?.data.plan_id === dr.planId) setPlan(null);
    status('元に戻した: ノートの長さ（未確定）');
    render();
    return Promise.resolve();
  }
  const it = lastPending();
  if (it) {
    it.canceled = true;
    const i = S.pending.indexOf(it);
    if (i >= 0) S.pending.splice(i, 1);
    try { it.cancel?.(); } catch { /* プレビューが消せなくても列からは外す */ }
    restorePreviews();
    invalidateWarp();
    status(`元に戻した: ${it.label}（まだ当てていなかった操作）`);
    render();
    return Promise.resolve();
  }
  return enqueue(() => historyStep('undo'));
}
export function redo() { return enqueue(() => historyStep('redo')); }

/** エンジンの undo / redo（曲の 1 本の履歴）。別のトラックの操作なら、エンジンがそのトラックに切り替える。 */
async function historyStep(kind) {
  const word = kind === 'undo' ? '元に戻した' : 'やり直した';
  if (await refuseWhileAwaiting()) return null;
  S.busy = true;
  renderToolbar();
  let hold = null;
  try {
    sentSeq += 1;                        // 呼んでいる間に読んだ描画データは採らない
    opActive = true;
    let r;
    try { r = await call(kind, {}); } finally { opActive = false; wake(); }
    const e = r.undone || r.redone || {};
    hold = await historyHandler(r);
    invalidate();
    await refresh({ keepView: true });
    const sw = r.switched_to ? `（編集対象を ${e.track_name || r.switched_to} に切り替え）` : '';
    status(`${word}: ${histLabel(e) || e.id || ''}${sw}`);
    return r;
  } catch (err) {
    if (!await handleEngineError(err)) {
      const none = /変更が無い/.test(err.message);
      status(none ? (kind === 'undo' ? '元に戻す操作が無い' : 'やり直す操作が無い')
        : `${kind === 'undo' ? '元に戻せなかった' : 'やり直せなかった'}: ${err.message}`);
      await refresh().catch(() => {});
    }
    return null;
  } finally {
    hold?.finish();
    S.busy = false;
    renderToolbar();
    render();
    wake();
  }
}
