// 編集の確定。**ドラッグ中はローカル、離した時点でエンジンのツールを呼ぶ**。
// 呼んだあとは必ず export_view_data で引き直して描き直す（画面は常にエンジンが真）。
//
// **取り消し（issue #16）**: 履歴はエンジンが持つ（セッションの 1 本の履歴。`session.py`）。
// Ctrl+Z / Ctrl+Y はエンジンの undo / redo を呼ぶだけ（トラックの操作も、Claude Code の編集も同じ履歴）。
// 画面が持つのは**まだエンジンに渡していない操作の列**（順番待ち）だけで、Ctrl+Z はまずそこから外す。
import { EngineError, analyzeTake, call, callJob, status, viewData } from './engine.js';
import { beginBusy } from './busy.js';
import { S, adopt, bandOf, fmtTime, histLabel, invalidateWarp, setPlan } from './state.js';
import { render, renderToolbar } from './draw.js';
import { invalidate } from './audio.js';
import { adoptSession } from './session.js';

const AUTHOR = 'human';

export async function refresh({ keepView = true, beforeRender = null } = {}) {
  if (awaiting()) { render(); return; }    // 準備を待つのをやめた: 終わるまで空のまま（描画データを頼むと待たされる）
  const vd = await viewData();
  adopt(vd, { keepView });
  restorePreviews();
  beforeRender?.();
  render();
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
  S.drag = null;
  S.stroke = null;
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

/** 離した操作を順番待ちに入れる。前に入れたものと S.busy（Ctrl+Z など）が終わってから fn を呼ぶ。
 * 待たされたときは、待っていることをステータスに出す（黙って止まって見えないように）。
 *
 * label（取り消しの名前。「元に戻す: ピッチ」）を渡した操作は、**当たる前なら Ctrl+Z で列から外せる**。
 * cancel: 外したときにプレビューを消す。preview: 描き直し（前の操作の確定）の後にプレビューを載せ直す。 */
const WAIT_MSG = '前の編集を当て終わったら続けて当てる…';
export function enqueue(fn, { label = null, cancel = null, preview = null, waitMsg = WAIT_MSG } = {}) {
  const wait = S.busy || S.queued > 0;
  if (wait) status(waitMsg);
  S.queued += 1;
  const item = { label, cancel, preview, canceled: false, started: false };
  queuedItems.add(item);
  if (label) S.pending.push(item);
  renderToolbar();
  const p = chain
    .then(() => waitFor(() => !S.busy))
    .then(() => {
      item.started = true;
      const i = S.pending.indexOf(item);
      if (i >= 0) S.pending.splice(i, 1);
      if (item.canceled) return null;
      return fn();
    })
    .finally(() => {
      queuedItems.delete(item);
      S.queued -= 1;
      // run を通らずに終わった（動かしていなかった・計画が作れなかった）ときに「待っている」を残さない
      if (!S.queued && document.querySelector('#status')?.textContent === WAIT_MSG) status('');
      renderToolbar();
      wake();
    });
  chain = p.catch(() => {});
  return p;
}

/** 順番待ちでまだ当たっていない操作のうち最後のもの（取り消しの名前つき）。 */
export function lastPending() {
  for (let i = S.pending.length - 1; i >= 0; i--) {
    if (!S.pending[i].started && !S.pending[i].canceled) return S.pending[i];
  }
  return null;
}

/** 当たっていない操作のプレビュー（ピッチの量など）を載せ直す。描き直し（adopt）で消えるため。
 * ピッチのドラッグ中はそのドラッグが順番待ちの分も足して持っているので、ピッチは触らない。 */
export const draggingPitch = () => S.drag?.type === 'note' && S.drag.axis === 'pitch';
export function restorePreviews() {
  if (!draggingPitch()) S.local.pitch.clear();
  for (const it of S.pending) if (!it.canceled) it.preview?.();
}

/** いままでに順番待ちに入れた操作が全部当たり、S.busy も外れたら解ける。 */
export function afterQueued() {
  return chain.then(() => waitFor(() => !S.busy));
}

/** 前の操作が全部当たり終わった（新しい計画を頼んでよい・外部の変更を読み直してよい）。 */
export const idle = () => !S.busy && !S.pendingPlan && S.queued === 0;

/** 編集を 1 つ当てる。fn は当たった呼び出しの数を数える関数（done）を受け取る。複数の呼び出しでできた操作は
 * 呼び出しが当たるたびに done() を呼ぶ（途中で失敗したら「一部だけ当たった可能性がある」と伝える）。 */
async function run(fn, label, beforeRender = null) {
  if (S.busy) return null;
  if (await refuseWhileAwaiting()) return null;
  S.busy = true;
  renderToolbar();
  // 呼び出しと描き直しをまとめて 1 本の線にする（描画データの読み直しは裏方なので、それだけでは出さない）
  const line = beginBusy({ label: `${label || '編集'}を反映している…` });
  let applied = 0;
  try {
    const r = await fn(() => { applied += 1; });
    invalidate();
    // 計画のプレビューは、新しい view data と**同じ描画で**差し替える（ちらつかせない）
    await refresh({ beforeRender });
    status('');
    return r;
  } catch (err) {
    const partial = applied > 0;
    if (!await handleEngineError(err, { partial })) {
      status(`${label || '編集'}に失敗: ${err.message}${partial ? PARTIAL_SUFFIX : ''}`);
      await refresh({ beforeRender }).catch(() => {});
    }
    return null;
  } finally {
    line.finish();
    S.busy = false;
    renderToolbar();
    wake();
  }
}

let groupSeq = 0;
/** blob の中央を上下 → ピッチ。半音スナップはしない（連続値）。複数ノートは取り消し 1 回にまとめる（group）。 */
export function applyPitch(deltas, { label = null } = {}) {
  const items = [...deltas.entries()].filter(([, d]) => Math.abs(d) > 1e-4);
  if (!items.length) { S.local.pitch.clear(); restorePreviews(); render(); return Promise.resolve(); }
  const group = `pitch-${Date.now().toString(36)}-${++groupSeq}`;
  return run(async (done) => {
    for (const [id, d] of items) {
      await call('shift_pitch', { note_id: id, cents: d * 100, author: AUTHOR, group, label });
      done();
    }
  }, label || 'ピッチの編集');
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
  const preview = () => { for (const [id, v] of deltas) S.local.pitch.set(id, (S.local.pitch.get(id) || 0) + v); };
  preview();
  render();
  return enqueue(() => {
    restorePreviews();                  // 前の操作の描き直しで消えたプレビューを載せ直してから当てる
    if (!draggingPitch()) preview();
    return applyPitch(deltas, { label: '半音に合わせる' });
  }, { label: '半音に合わせる', preview, cancel: () => {} });
}

/** 結合（Ctrl+J）: 接して並ぶノートをまとめて 1 つに（取り消し 1 回）。ids は並び順。 */
export function mergeMany(ids) {
  if (ids.length < 2) return Promise.resolve();
  const group = `merge-${Date.now().toString(36)}-${++groupSeq}`;
  return enqueue(() => run(async (done) => {
    for (let k = 1; k < ids.length; k++) {
      await call('merge_notes', { note_a: ids[0], note_b: ids[k], author: AUTHOR, group });
      done();
    }
    S.sel = [ids[0]];
  }, 'ノートの結合'), { label: '結合' });
}

/** 無音にする（Del）／無音を戻す（右クリック）。長さ・位置は変えず、戻すのは無音だけ（ピッチ等の編集は残る）。
 * ids は 1 回の呼び出し = 1 つの changeset（取り消し 1 回）。ミュートツールは、なぞっている間の見かけを
 * S.local.mute に持ち、当たるまでは preview で載せ直す。 */
function applyMute(ids, to) {
  if (!ids.length) return Promise.resolve();
  const label = to ? '無音にする' : '無音を戻す';
  const preview = () => { for (const id of ids) S.local.mute.set(id, to); };
  return enqueue(() => run(async () => {
    const r = await call(to ? 'mute_notes' : 'unmute_notes', { note_ids: ids, author: AUTHOR });
    if (!r.changeset) status(to ? 'もう無音になっている' : '無音のノートが無かった');
  }, label), { label, preview, cancel: () => { for (const id of ids) S.local.mute.delete(id); } });
}
export const muteNotes = (ids) => applyMute(ids, true);
export const unmuteNotes = (ids) => applyMute(ids, false);

/** フェード（issue #20）: `set_fade`。fadeIn / fadeOut は編集後の秒（省くとそのまま、0 で消す）。**順番待ちの中から呼ぶ**。 */
export function applyFade(ids, { fadeIn = null, fadeOut = null } = {}) {
  return run(async () => {
    const r = await call('set_fade', { note_ids: ids, fade_in_sec: fadeIn, fade_out_sec: fadeOut, author: AUTHOR });
    return r;
  }, 'フェード');
}

/** フェードを消す（右クリック）。 */
export function clearFades(ids) {
  if (!ids.length) return Promise.resolve();
  return enqueue(() => applyFade(ids, { fadeIn: 0, fadeOut: 0 }), { label: 'フェードを消す' });
}

/** 境目の接続／切り離し（右クリックの「切り離す」「つなぐ」。音は変わらない）。 */
export function setConnection(a, b, connected) {
  return enqueue(() => run(
    () => call('set_connection', { note_a: a, note_b: b, connected, author: AUTHOR }),
    connected ? 'つなぐ' : '切り離し'), { label: connected ? 'つなぐ' : '切り離し' });
}

/** 隙間のある境目を「つなぐ」: 前のノートの尻を次のノートの頭まで伸ばして接続する
 * （端を隣まで伸ばして吸着させるドラッグと同じ計画・同じ確定）。 */
export function connectGap(a) {
  return enqueue(async () => {
    const data = await requestPlan({ op: 'edge', note_id: a, side: 'end', detach: false });
    if (!data) return;
    if (data.snap_x == null) { status('隣のノートまで伸ばせない'); return; }
    await applyPlan({ planId: data.plan_id, x: data.snap_x, label: 'つなぐ' });
  }, { label: 'つなぐ' });
}

/** 子音｜母音の境目を元に戻す（その音素の両側の境目の move_boundary を外す）。 */
export function resetBoundaries(ids) {
  if (!ids.length) return Promise.resolve();
  return enqueue(() => run(
    () => call('reset_to_original', { boundary_ids: ids, author: AUTHOR }), '音素の境目を元に戻す'),
  { label: '音素の境目を元に戻す' });
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
 * （取り消しの履歴でも前回の項目と入れ替わる = ポップアップ 1 回 = 取り消し 1 回）。 */
export async function applyPlan({ planId, x = null, pitch = null, replaces = null, drag = true, label = null }) {
  let ok = false;
  const after = () => {
    // ドラッグは成功しても失敗してもプレビューを外す。ただし**この計画のときだけ**
    // （順番待ちの間に「ガイドに合わせる」を開いていたら、その計画は残す）
    if (drag) { if (S.plan?.data.plan_id === planId) setPlan(null); }
    else if (S.plan && ok && S.plan.data.plan_id === planId) {   // 表示中の view data がこの値まで当たっている
      S.plan.x0 = x || 0;                // （スライダーの今の値ではなく、確定した値）
      S.plan.pitch0 = pitch || 0;
      invalidateWarp();
    }
  };
  if (!(Math.abs(x || 0) > 1e-9) && !(Math.abs(pitch || 0) > 1e-9) && !replaces) {
    ok = true;
    after();
    render();
    return { ok: true, changeset: null };
  }
  const cs = await run(async () => {
    const r = await call('apply_plan', {
      plan_id: planId, x, pitch, replaces, author: AUTHOR, label,
    });
    ok = true;
    if (r.snapped) status('接続した');
    else if (r.clamped) status('隣にぶつかる手前で止めた');
    return r.changeset || null;
  }, drag ? 'タイミングの編集' : 'ガイドに合わせる', after);
  return { ok, changeset: cs };
}

/** 鉛筆で描いた線 → `set_pitch_curve(mode="draw")`。points = [[編集前の秒, MIDI], ...]。
 *
 * 範囲はエンジンが有声のフレームに切り詰める（無声には音程が無いので描いても効かない）。
 * 確定後の view data で描き直すまで、描いた線（S.stroke）のプレビューを残す（ちらつかせない）。 */
export function applyDraw(points, stroke = S.stroke) {
  return run(async () => {
    const r = await call('set_pitch_curve', { points, mode: 'draw', author: AUTHOR });
    return r;
  }, 'ピッチを描く', () => { if (S.stroke === stroke) S.stroke = null; });   // 待っている間に描き始めた線は消さない
}

/** はさみ: sec（編集前の秒）でノートを分ける。吸着は画面側で済ませた時刻を渡す。 */
export function splitNote(noteId, sec) {
  return enqueue(() => run(
    () => call('split_note', { sec, note_id: noteId, author: AUTHOR }), 'ノートの分割'),
  { label: '分割' });
}

/** はさみ: 境目のダブルクリックで結合。 */
export function mergeNotes(a, b) {
  return enqueue(() => run(
    () => call('merge_notes', { note_a: a, note_b: b, author: AUTHOR }), 'ノートの結合'),
  { label: '結合' });
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
  }, 'つなぎのなだらかさ');
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
    });
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
  }, '歌詞の設定'), { label: '歌詞' });
}

/** ピッチ検出の方式を替えて、開いているトラックを解析し直す（f0.js。ユーザー設定なので取り消しの履歴には入れない）。
 * onChanged: set_f0_estimator の結果を受け取る。失敗・取り消しのときは null を返す。 */
export function setF0Estimator(estimator, onChanged) {
  return enqueue(() => run(async () => {
    onChanged?.(await call('set_f0_estimator', { estimator }));
    status('ピッチを解析し直している…');
    await analyzeTake({ label: 'ピッチを解析し直している', target: S.take?.name || '',
      onProgress: (sec) => status(`ピッチを解析し直している… ${sec} 秒`) });
    return true;
  }, 'ピッチ検出の方式'));
}

/** 歌詞レーンの 1 音節を直す。エンジンの changeset なので Ctrl+Z で戻せる。 */
export function setNoteSyllable(noteId, syllableIndex, kana) {
  return enqueue(() => run(async () => {
    status('音節を直している…');
    await call('set_note_syllable', { note_id: noteId, syllable_index: syllableIndex,
      kana, author: AUTHOR });
  }, '音節の修正'), { label: '音節' });
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
  }, '歌詞の読み込み'), { label: '歌詞' });
}

/** 譜面と録音の対応をエンジンに判定させ、確かな区間だけ歌詞として確定する。 */
export function loadLyricsScore(path, track) {
  return enqueue(() => run(async () => {
    status('譜面と発音の位置を照合している…');
    return call('import_lyrics', { path, track, author: AUTHOR, reanalyze: true });
  }, '譜面の読み込み'), { label: '歌詞' });
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
    await call('reset_to_original', { note_ids: ids, author: AUTHOR });
  }, 'オリジナルに戻す'), { label: 'オリジナルに戻す' });
}

/** Ctrl+Z: **まだ当たっていない操作があれば列から外すだけ**（画面からすぐ消える。エンジンは呼ばない）。
 * 無ければエンジンの履歴の最後の操作を取り消す（順番待ちに入れる。確定中の操作を追い越さない）。 */
export function undo() {
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
    const r = await call(kind, {});
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
