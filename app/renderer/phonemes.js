// 歌詞の音素の解析（2026-10-10 承認）。
//
// 歌詞を直しても、エンジンは変わった区間（フレーズ）だけを裏で解析し、編集を待たせない。
// 解析していない区間は、歌詞レーンに点線の枠で「解析中…」（裏で解析している）・「未解析」（手動の設定）と出す（draw.js）。
// 裏の解析は描画データに載らないので、未解析の区間がある間だけ phoneme_status（ロックを取らない）を読み、
// 区間が解析されたら描き直す。
//
// 編集 > 歌詞と音素を自動で解析する（既定はオン。state.json に保存し、起動時に GLISS_AUTO_PHONEMES でエンジンに渡す）。
// オフにすると、曲を開いても歌詞を直しても音素を解析せず、「音素を解析」を押したときだけ解析する。
import { S } from './state.js';
import { call, status } from './engine.js';
import { onRender, render } from './draw.js';
import { enqueue, handleEngineError, idle, refresh } from './edits.js';

const POLL_MS = 800;
const P = { auto: true, state: null, gen: null };
let timer = null;
let polling = false;
let seenVd = null;              // 最後に状態を読んだ描画データ（同じ描画データのまま読み続けない）
let staleGen = null;            // 描画データが古いとみて取り直した gen（同じ gen では 1 回だけ）
let changed = () => {};

export const phonemeState = () => ({ ...P, pending: [...pendingEntries()] });
export const autoPhonemes = () => P.auto;
export function onAutoPhonemesChange(fn) { changed = fn; }

/** 表示中のトラックの、音素をまだ解析していない歌詞の区間の番号。 */
export const pendingEntries = () => S.ph?.pending_entries || [];
/** 裏で解析している（順番待ちを含む）。 */
export const analyzing = () => P.state === 'queued' || P.state === 'preparing';

function adopt(r) {
  if (!r || r.ok === false) return;
  if (typeof r.auto === 'boolean' && r.auto !== P.auto) { P.auto = r.auto; changed(); }
  if ('state' in r) P.state = r.state;
}

function schedule(ms = POLL_MS) {
  if (timer || polling) return;
  timer = setTimeout(poll, ms);
}

async function poll() {
  timer = null;
  if (polling) return;
  polling = true;
  seenVd = S.vd;
  const was = { state: P.state, gen: P.gen };
  let r = null;
  try {
    r = await call('phoneme_status', {}, { busy: false });
  } catch {
    r = null;
  } finally {
    polling = false;
  }
  if (!r) { if (pendingEntries().length) schedule(POLL_MS * 3); return; }
  adopt(r);
  P.gen = r.gen ?? null;
  // 区間を解析した（gen が進んだ）・裏の解析が終わった: 描き直す。操作の途中なら終わるまで待つ。
  // エンジンの側ではもう未解析の区間が無いのに、描画データに残っている（解析が済んだ後に読んだ）ときも取り直す
  const stale = !analyzing() && pendingEntries().length && Array.isArray(r.pending_entries) && !r.pending_entries.length
    && staleGen !== P.gen;
  if (stale) staleGen = P.gen;
  const done = (was.gen != null && P.gen !== was.gen) || (was.state !== P.state && !analyzing()) || stale;
  if (done) await redrawWhenIdle();
  else render();
  if (pendingEntries().length && analyzing()) schedule();
}

async function redrawWhenIdle() {
  while (!idle() || S.drag) await new Promise((res) => setTimeout(res, 200));
  await refresh().catch(() => {});
}

/** 「音素を解析」: 表示中のトラックの未解析の区間を、設定によらず今すぐ解析する（歌詞が無ければ推定してから）。 */
export function analyzePhonemesNow() {
  if (!S.take) { status('音素を解析するトラックが無い'); return Promise.resolve(null); }
  return enqueue(async () => {
    try {
      const r = await call('analyze_phonemes', {}, { busy: '音素を解析している…' });
      adopt({ state: r.phonemes_state === 'analyzing' ? 'queued' : P.state });
      await refresh();
      if (r.phonemes_state === 'no_lyrics') status('歌詞が無いので音素を解析できない（歌詞レーンをダブルクリックして入れる）');
      else if (r.phonemes_state === 'analyzing') status('音素を裏で解析している（終わった区間から表示する）');
      else status('音素を解析した');
      schedule(POLL_MS);
      return r;
    } catch (err) {
      if (!await handleEngineError(err)) status(`音素を解析できなかった: ${err.message}`);
      return null;
    }
  });
}

/** 「歌詞と音素を自動で解析する」を切り替える（次に起動したときもこの設定）。 */
export async function setAutoPhonemes(on) {
  const r = await call('set_auto_phonemes', { enabled: !!on }, { busy: false });
  adopt(r);
  await window.api?.saveState?.({ autoPhonemes: P.auto });
  status(P.auto ? '歌詞と音素を自動で解析する（歌詞を直すと、変わった区間を裏で解析する）'
    : '歌詞と音素を自動で解析しない（「音素を解析」を押したときだけ解析する）');
  changed();
  if (pendingEntries().length) schedule(0);
  return P.auto;
}
export const toggleAutoPhonemes = () => setAutoPhonemes(!P.auto);

/** 起動時: エンジンの設定を読む。描き直しのたびに、未解析の区間があれば状態を読む。 */
export async function installPhonemes() {
  onRender(() => {
    if (pendingEntries().length && (S.vd !== seenVd || analyzing())) schedule();
  });
  try { adopt(await call('phoneme_status', {}, { busy: false })); } catch { /* 読めなければ既定のまま */ }
}
