// 聞き取り（区間の音声認識で歌詞を確かめる。issue #54）。
//
// 歌詞レーンの区間（歌詞のある区間、無ければ発声のかたまり）を右クリック →「聞き取る」。
// 結果は**候補**で、確定の歌詞は変えない。歌詞レーンのその場に薄く出し（draw.js）、
//   Enter = 採用（その区間の確定の歌詞にする。set_lyrics なので Ctrl+Z で戻せる）
//   ダブルクリック = 直してから採用（歌詞の入力欄に候補を入れて開く。interact.js）
//   Esc = 候補を捨てる（聞き取っている最中の Esc は聞き取りを取り消す）
// 重みは同梱しない: 初めて使うときだけ、大きさ・保存先・ライセンスを確かめてからダウンロードする（取り消せる）。
// faster-whisper が無い環境では「聞き取る」を無効にし、理由を出す（asr_status の reason）。
import { LAYOUT, S, lyricEntryAt, toEdited, toSource, utteranceAt } from './state.js';
import { X, render, rollBottom } from './draw.js';
import { callJob, status } from './engine.js';
import { beginBusy } from './busy.js';
import { setLyrics } from './edits.js';

let info = null;              // asr_status の結果（無ければまだ問い合わせていない）
let asking = null;
let running = null;           // { cancel } 聞き取りのジョブ

/** エンジンに使えるかを問い合わせ直す（起動時・取得の後）。probe: GPU を使えるかも調べる。 */
export async function refreshAsr({ probe = false } = {}) {
  const p = window.api.call('asr_status', probe ? { probe: true } : {}).then((r) => {
    info = r && r.ok !== false ? r : { available: false, reason: r?.error || 'エンジンが答えない' };
    return info;
  }).catch((e) => { info = { available: false, reason: String(e?.message || e) }; return info; });
  asking = p;
  return p;
}

export const asrInfo = () => info;
/** 「聞き取る」が使えるか（faster-whisper が入っている）。 */
export const asrReady = () => !!info?.available;
/** 使えない理由（メニューのツールチップ・キーを押したときのステータス）。 */
export function asrWhy() {
  if (!info) return 'エンジンに問い合わせている';
  return info.reason || '発声のある所で使う';
}

/** 聞き取る区間（編集前の秒）: 歌詞のある区間ならそれ、無ければ発声のかたまり。 */
export function asrRangeAt(tSrc) {
  const ent = lyricEntryAt(tSrc);
  if (ent && !ent.whole) return { start_sec: ent.start_sec, end_sec: ent.end_sec };
  const u = utteranceAt(tSrc);
  return u ? { start_sec: +u[0].toFixed(3), end_sec: +u[1].toFixed(3) } : null;
}

/** キー・メニューバーから（右クリックの位置が無い）: 選んだノート、無ければ再生位置。 */
export function defaultAsrTime() {
  const n = S.pitched.find((x) => S.sel.includes(x.id));
  if (n) return (n.start_sec + n.end_sec) / 2;
  return toSource(S.head - S.off);
}

const trackId = () => S.session?.current || null;
const mine = (x) => !!x && x.trackId === trackId();
/** 表示中のトラックの候補（別のトラックに切り替えたら出さない）。 */
export const asrCandidate = () => (mine(S.asrCand) ? S.asrCand : null);
export const asrRunning = () => (mine(S.asrRunning) ? S.asrRunning : null);

/** 区間の上（歌詞レーンの上端）の、進み具合を出す位置（画面の座標）。 */
function laneBar(r) {
  const svg = document.querySelector('#roll');
  if (!svg) return null;
  const b = svg.getBoundingClientRect();
  const x0 = Math.max(LAYOUT.KEYS_W, X(toEdited(r.start_sec)));
  const x1 = Math.min(b.width, X(toEdited(r.end_sec)));
  if (x1 <= x0) return null;
  return { left: b.left + x0, top: b.top + rollBottom() + 1, width: x1 - x0 };
}

function summary(c) {
  const parts = [`聞き取り: 「${c.text || '（聞き取れない）'}」`];
  if (c.kana && c.kana !== c.text) parts.push(`（${c.kana}）`);
  if (c.confidence) parts.push(` 確信 ${c.confidence.value.toFixed(2)}`);
  const cur = c.current;
  if (cur?.kana) parts.push(cur.same ? '・今の歌詞と同じ' : `・今の歌詞と ${cur.char_errors} 字違う`);
  parts.push(c.lyrics ? '。Enter で採用・ダブルクリックで直す・Esc で取り消し' : '。Esc で閉じる');
  if (c.warnings?.length) parts.push(`（${c.warnings.slice(0, 2).join('／')}）`);
  return parts.join('');
}

/** その位置の区間を聞き取る（右クリックの「聞き取る」）。 */
export async function transcribeAt(tSrc) {
  if (asrRunning()) { status('聞き取っている途中（Esc で取り消し）'); return null; }
  const r = asrRangeAt(tSrc);
  if (!r) { status('ここには発声が無い（発声のある所で聞き取る）'); return null; }
  const st = await refreshAsr({ probe: true });
  if (!st.available) { status(`聞き取る: 使えない（${asrWhy()}）`); return null; }
  if (!st.installed) {
    const m = st.model;
    if (!await window.api.confirmAsrDownload(m)) { status('聞き取りのモデルは取得しなかった'); return null; }
    try {
      await callJob('prepare_asr_model', { model: m.id }, null,
        { label: '聞き取りのモデルを取得している', target: `${m.id}（${m.size_text}）`, modal: true });
    } catch (e) {
      if (e.cancelled) { status('モデルの取得を取り消した（途中のファイルは消した）'); return null; }
      throw e;
    }
    await refreshAsr({ probe: true });
    status('聞き取りのモデルを取得した');
  }
  S.asrCand = null;
  S.asrRunning = { ...r, trackId: trackId() };
  const cpu = info?.device === 'cpu';
  const label = cpu ? '聞き取っている…（GPU を使えないので CPU で処理。遅い）' : '聞き取っている…';
  const task = beginBusy({ label, anchor: () => laneBar(r) });
  running = { cancel: null };
  const busy = { ...task, cancelWith(fn, opts) { running.cancel = fn; task.cancelWith(fn, opts); } };
  status(label + (cpu && info.device_note ? `（${info.device_note}）` : ''));
  render();
  try {
    const res = await callJob('transcribe', { start_sec: r.start_sec, end_sec: r.end_sec,
      source: 'take', background: true },
    (sec) => status(`${label} ${sec} 秒`), { busy });
    const cand = { ...res, start_sec: r.start_sec, end_sec: r.end_sec, trackId: S.asrRunning?.trackId };
    S.asrCand = cand;
    status(summary(cand));
    return cand;
  } catch (e) {
    if (e.cancelled) { status('聞き取りを取り消した'); return null; }
    throw e;
  } finally {
    task.finish();
    running = null;
    S.asrRunning = null;
    render();
  }
}

/** 候補を採用する（確定の歌詞にする）。text を渡すと直した文で。取り消せる（「元に戻す: 歌詞」）。 */
export function acceptCandidate(text) {
  const c = asrCandidate();
  if (!c) return null;
  const t = (text ?? c.lyrics ?? '').trim();
  if (S.lyricsEntries.some((e) => e.start_sec == null)) {
    // 素材全体で 1 つの歌詞（区間に分かれていない）を、区間の歌詞で置き換えて消さない
    status('素材全体で 1 つの歌詞があるので、区間の候補は採用しない（歌詞を編集… で直す）');
    return null;
  }
  S.asrCand = null;
  render();
  if (!t) { status('空の候補は採用しない'); return null; }
  status(`聞き取りの候補を歌詞にした: ${t}`);
  return setLyrics(t, { start_sec: c.start_sec, end_sec: c.end_sec });
}

/** 候補を捨てる。 */
export function dismissCandidate() {
  if (!S.asrCand) return false;
  S.asrCand = null;
  status('聞き取りの候補を閉じた');
  render();
  return true;
}

/** 候補の区間の中か（編集前の秒）。 */
export function inCandidate(tSrc) {
  const c = asrCandidate();
  return !!c && tSrc >= c.start_sec && tSrc <= c.end_sec;
}

const TEXT_INPUT = /^(text|search|number|email|password|url|tel)$/i;
function typing(el) {
  if (!el) return false;
  if (el.isContentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
  return el.tagName === 'INPUT' && TEXT_INPUT.test(el.type || 'text');
}

/** Enter（採用）・Esc（捨てる・聞き取りの取り消し）。ほかのキー（コマンドの表）より先に見る。 */
export function installAsr({ dialogOpen = () => false } = {}) {
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || dialogOpen() || typing(e.target)) return;
    if (e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
    const menu = document.querySelector('#menu');
    if (menu && !menu.hidden) return;
    if (e.key === 'Escape' && asrRunning() && running?.cancel) {
      e.preventDefault();
      e.stopImmediatePropagation();
      const fn = running.cancel;
      running.cancel = null;
      status('聞き取りを取り消している…');
      Promise.resolve(fn()).catch(() => {});
      return;
    }
    if (!asrCandidate()) return;
    if (e.key === 'Enter' && e.target?.tagName !== 'BUTTON') {
      e.preventDefault();
      e.stopImmediatePropagation();
      const p = acceptCandidate();
      if (p && typeof p.catch === 'function') p.catch((err) => status(`歌詞にできなかった: ${err.message}`));
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopImmediatePropagation();
      dismissCandidate();
    }
  }, true);
  refreshAsr();
}

/** テスト用: 問い合わせ中なら待つ。 */
export const asrSettled = () => asking || Promise.resolve(info);
