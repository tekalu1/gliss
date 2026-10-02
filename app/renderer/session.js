// トラック（セッション）の状態を画面に取り込み、トラックの操作をエンジンに渡す（issue #7）。
//
// エンジンが真: 操作はすべて MCP のツール（set_track / select_track / add_track …）を通し、
// 返ってきた `session`（list_tracks と同じ形）を S.session / S.tracks に取り込み直す。
import { call } from './engine.js';
import { S, syncOff } from './state.js';
import { setGains } from './audio.js';

const hooks = [];
/** セッションを取り込んだあとに呼ぶもの（トラックビューの描き直しなど）。 */
export function onSession(fn) { hooks.push(fn); }

/** エンジンの返り値の `session` を取り込む。 */
export function adoptSession(sess) {
  if (!sess || !Array.isArray(sess.tracks)) return;
  S.session = sess;
  S.tracks = sess.tracks;
  if (sess.history) S.hist = sess.history;        // 曲の取り消しの履歴の要約（issue #16）
  syncOff();
  setGains();
  for (const fn of hooks) fn(sess);
}

/** ミュート・ソロ・名前などを変える（位置は tracks.js の offset の確定で）。 */
export async function setTrack(id, patch) {
  const ms = Object.keys(patch).every((k) => k === 'mute' || k === 'solo');
  if (!ms) {
    const r = await call('set_track', { track_id: id, ...patch });
    adoptSession(r.session);
    return r;
  }
  // ミュート／ソロ: 聴き比べの操作なので順番待ちに入れず、すぐ効かせる（取り消しの履歴にも入らない）。
  // 返ってきたセッションは丸ごとは取り込まない（順番待ちの操作 = 編集対象の切り替えなどと入れ違うと、
  // 古い編集対象に戻ってしまう）。ミュート／ソロだけ写す
  const t = S.tracks.find((x) => x.id === id);
  if (t) { Object.assign(t, patch); setGains(); for (const fn of hooks) fn(S.session); }
  const r = await call('set_track', { track_id: id, ...patch });
  const by = new Map((r.session?.tracks || []).map((x) => [x.id, x]));
  for (const x of S.tracks) {
    const y = by.get(x.id);
    if (y) { x.mute = y.mute; x.solo = y.solo; x.audible = y.audible; }
  }
  setGains();
  for (const fn of hooks) fn(S.session);
  return r;
}

// ---------------------------------------------------------------- ガイドが重ならない理由（issue #32）
// エンジンは編集中のトラックにガイドが重ならない理由を session.guide_note で返す。画面は黙って重ねないのではなく、
// ステータス行と「ガイドに合わせる」の使えない理由に出す（ユーザーが理由を知らずにトラックを外して回らないように）
const NOTE_TEXT = {
  編集対象がガイドのトラック自身: '編集中のトラックがガイド。別のトラックを選ぶと重なる',
  ガイドが指定されていない: 'トラックの見出しのガイドのアイコンでガイドを指定する',
  ガイドが伴奏になっている: 'ガイドのトラックが伴奏になっている。ボーカルに戻すか別のトラックをガイドにする',
};

/** 編集中のトラックにガイドが重なっている（ガイドの解析と対応付けまで済んでいる。view data の guide_basis）。 */
export function guideShown() {
  return !!(S.vd?.guide && S.vd?.guide_basis);
}

/** ガイドが重ならない理由（重なっていれば null）。 */
export function guideWhy() {
  if (guideShown()) return null;
  if (!S.tracks.length) return 'ガイドを開いてから';
  const n = S.session?.guide_note;
  if (n) return NOTE_TEXT[n] || n;
  if (S.session?.guide) return 'ガイドを解析できていない（ステータス行を見る）';
  return NOTE_TEXT.ガイドが指定されていない;
}

/** ステータス行の「 / ガイド: 名前」（重ならないときは理由、対応付けに失敗して位置のまま重ねたときはその旨）。 */
export function guideSuffix() {
  if (guideShown()) {
    const w = S.vd?.guide_warning ? '（対応付けに失敗したので位置のまま重ねている）' : '';
    return ` / ガイド: ${S.guide.name}${w}`;
  }
  if (!S.session?.guide && !S.guide) return '';
  return `（ガイドは重ならない: ${guideWhy()}）`;
}

/** ステータス行の「（音素を切れなかった区間 N か所…）」。歌詞のうち音素アラインに失敗した区間は音素なしで
 * 続けている（再生・戻す・ガイドに合わせるは使える）ことを知らせる（issue #58）。 */
export function phonemeSuffix() {
  const ph = S.vd?.phonemes;
  if (!ph) return '';
  if (ph.error) return '（音素を切れなかった。音素なしで続けている）';
  const n = (ph.warnings || []).filter((w) => w.kind === 'align_failed').length;
  return n ? `（音素を切れなかった区間 ${n} か所。その区間は音素なしで続けている）` : '';
}

// ---------------------------------------------------------------- 開いているプロジェクト（issue #33）
// エンジンのツールの返り値の `document`（`project_status` と同じ形: kind・path・name・dirty・work_dir）を取り込み、
// タイトル（「名前* — Gliss」）とメニュー（保存）に反映する。engine.js が返り値を見るたびに呼ぶ
const docHooks = [];
/** プロジェクトの状態が変わったあとに呼ぶもの（メニューの作り直しなど）。 */
export function onDoc(fn) { docHooks.push(fn); }

export function adoptDoc(d) {
  const next = d ? { kind: d.kind, path: d.path || null, name: d.name || '無題', dirty: !!d.dirty,
    work_dir: d.work_dir || null, tracks: d.tracks || 0 } : null;
  const same = JSON.stringify(next) === JSON.stringify(S.doc || null);
  S.doc = next;
  if (same) return;
  // タイトル: 「名前* — Gliss」（未保存なら *）。ページの title とウィンドウのタイトルの両方（main も同じ形で付ける）
  document.title = next ? `${next.name}${next.dirty ? '*' : ''} — Gliss` : 'Gliss';
  window.api?.setDoc?.(next ? { name: next.name, dirty: next.dirty, kind: next.kind, path: next.path } : null);
  for (const fn of docHooks) fn(next);
}

/** エンジンからセッションを読み直す（外部が変えた・起動直後）。 */
export async function loadSession() {
  const r = await call('list_tracks', {});
  adoptSession(r);
  return r;
}
