// エンジン（MCP ツール）の呼び出し。**画面も AI も同じツールを通す**ので、
// ここから先はすべて `window.api.call(name, args)` に落ちる。
export const $ = (s) => document.querySelector(s);
import { beginBusy, laterBusy } from './busy.js';
import { S } from './state.js';

let statusEl = null;
let pendingStatus = null;
export function status(text) {
  statusEl = statusEl || $('#status');
  clearTimeout(pendingStatus);
  if (!statusEl) return;
  if (/(?:している|読み込んでいる|開いている|作っている).*…/.test(text || '')) {
    pendingStatus = setTimeout(() => { statusEl.textContent = text; }, 300);
  } else {
    statusEl.textContent = text || '';
  }
}

export class EngineError extends Error {
  constructor(message, result = {}) {
    super(message);
    this.name = 'EngineError';
    this.conflict = result.conflict === true;
    this.preparing = result.preparing === true;
  }
}

/** null / undefined の引数は**送らない**。
 *
 * エンジン側のツールは `guide_path: str = None` のように「省略できる文字列」で
 * 書いてあり、MCP のスキーマ検証は明示的な `null` を弾く
 * （`Input should be a valid string [input_value=None]`）。省略と null を
 * 呼び出し側で区別しなくて済むよう、ここで落とす。 */
function clean(args) {
  const out = {};
  for (const [k, v] of Object.entries(args || {})) {
    if (v !== null && v !== undefined) out[k] = v;
  }
  return out;
}

// 返り値に付いてくる、開いているプロジェクトの状態（issue #33。未保存か）を受け取るもの（session.js の adoptDoc）
let docHook = null;
export function onDocument(fn) { docHook = fn; }

// 0.3 秒を超えたら線を出す呼び出しの名前（ユーザーの操作の反映）。ここに無いものは「処理している…」
const LABELS = {
  shift_pitch: 'ピッチを反映している…', apply_plan: '編集を反映している…', set_pitch_curve: '描いたピッチを反映している…',
  split_note: 'ノートを分けている…', merge_notes: 'ノートを結合している…', mute_notes: '無音にしている…', unmute_notes: '無音を戻している…',
  set_fade: 'フェードを反映している…', set_connection: 'つなぎを反映している…',
  set_transition: 'つなぎのなだらかさを反映している…', move_boundary: '音素の境目を動かしている…',
  reset_to_original: 'オリジナルに戻している…', set_lyrics: '歌詞を反映している…', set_note_syllable: '音節を直している…',
  import_lyrics: '譜面と照合している…', set_tempo: 'テンポを反映している…', set_track: 'トラックを変えている…',
  split_track: 'クリップを分けている…', join_track: 'クリップをつないでいる…', mute_track_range: '部分を反映している…',
  add_track: 'トラックを足している…', remove_track: 'トラックを外している…', set_guide_track: 'ガイドを変えている…',
  set_track_guide: 'ガイドを変えている…',
  select_track: 'トラックを切り替えている…', save_project: '保存している…', undo: '元に戻している…', redo: 'やり直している…',
  render_tracks: '再生の音を作っている…', export_wav: '書き出している…',
};
// 線を出さない呼び出し（ユーザーの操作ではない裏方: 描画データ・一覧・状態のポーリング・計画・プレビュー音。#63）。
// 開く・解析・書き出しのように長いものは、呼び出し側が止める処理のポップアップ（busy.js）で見せる
const QUIET = new Set([
  'export_view_data', 'list_tracks', 'track_overview', 'prep_status', 'get_job', 'cancel_job', 'engine_info',
  'asr_status', 'project_status', 'plan_edit', 'render_audition', 'inspect_lyrics_score', 'close_project',
  'open_project', 'new_project', 'load_project', 'analyze_take', 'set_f0_estimator',
]);

/** ツールを 1 つ呼ぶ。エンジンは失敗も JSON で返すので、ここで例外に変える。
 * busy: 線のラベル（省けば呼び出しの名前から。裏方の呼び出しは出さない）。false で出さない。 */
export async function call(name, args, { busy: label } = {}) {
  const quiet = label === false || (label === undefined && QUIET.has(name));
  const busy = quiet ? null : beginBusy({ label: label || LABELS[name] || '処理している…' });
  let r;
  try { r = await window.api.call(name, clean(args)); }
  finally { busy?.finish(); }
  if (r && r.ok === false) throw new EngineError(`${name}: ${r.error || '処理できなかった'}`, r);
  if (r && typeof r === 'object' && docHook) {
    const d = 'document' in r ? r.document : (r.session && 'document' in r.session ? r.session.document : undefined);
    if (d !== undefined) docHook(d);
  }
  if (r && r.ok === undefined && typeof r.text === 'string') {
    throw new EngineError(`${name}: ${r.text}`);   // スキーマ検証エラーなど
  }
  return r;
}

// 取り消しを出さない待ち（ガイドの指定・Ctrl+Z・トラックを外す・保存・外部の変更・歌詞の後の解析）でも、
// これだけ待ったらポップアップに「待つのをやめる（Esc）」を出す。15 秒はエンジンの同期の待ちの上限
// （`prep.FRONT_WAIT_SEC`。Claude Code からの呼び出しはここで「準備中」を返す）と同じで、短い素材の準備・解析は
// ふつうその前に終わる。長い曲の初めての準備はこれを超えることがあり、進んでいるなら待てば済むので、
// 自動では打ち切らず、抜ける道だけを出す（別のエンジンが準備のロックを握ったまま・準備が進まないとき）。
let abandonMs = 15000;
/** テスト用: 「待つのをやめる」を出すまでのミリ秒。 */
export function setAbandonAfter(ms) { abandonMs = ms; }

// 待つのをやめた（callJob の abandoned）ときの後始末（tracks.js が入れる。表示を空にして、準備が終わったら描き直す）
let abandonHook = null;
export function onAbandon(fn) { abandonHook = fn; }

/** ジョブの実測進捗を受け取り、安全な境界での中断も待つ。
 *
 * options.busy: 進み具合を出す先。省くと線（label・modal・clipId）。`laterBusy()`（busy.js）を渡すと、**ジョブに
 * なったときだけ**止める処理のポップアップを始める（その場で返れば何も出さない。終わらせるのは呼び出し側）。
 * options.stage: ジョブが段階の名前を返さないときに出す名前。options.cancel: false で取り消しを出さない。
 * 取り消しを出さないときも、`abandonMs` を超えたら「待つのをやめる（Esc）」を出す（options.abandon: false で出さない）。
 * やめたら、ジョブは取り消せるなら取り消し（準備への合流なら準備そのものは続く）、abandoned の付いた EngineError を投げる。 */
export async function callJob(name, args, onProgress, options = {}) {
  const lazy = typeof options.busy?.start === 'function';
  const own = !options.busy;
  let busy = lazy ? null : (options.busy || beginBusy({ label: options.label || LABELS[name] || '処理している…',
    modal: !!options.modal, clipId: options.clipId || null, target: options.target || '' }));
  const cancellable = options.cancel !== false;
  const mayAbandon = !cancellable && options.abandon !== false;
  try {
    const r = await call(name, args, { busy: false });
    if (r.status !== 'running' || !r.job_id) return r;
    if (lazy) busy = options.busy.start();
    if (!busy) return r;
    const cancel = async () => {
      await window.api.call('cancel_job', { job_id: r.job_id });
    };
    let last = r;
    let gaveUp = false;
    let stop = null;
    const stopped = new Promise((res) => { stop = res; });
    const abandon = async () => {
      gaveUp = true;
      stop();
      if (last.cancellable) await window.api.call('cancel_job', { job_id: r.job_id }).catch(() => {});
    };
    const giveUpError = () => {
      const err = new EngineError(`${name}: 待つのをやめた`);
      err.abandoned = true;
      err.joined = !!(last.joined ?? r.joined);
      return err;
    };
    busy.update(r.progress, options.stage || null);
    busy.cancelWith(cancellable && r.cancellable ? cancel : null);
    const t0 = Date.now();
    let offered = false;
    for (;;) {
      await Promise.race([new Promise((res) => setTimeout(res, 200)), stopped]);
      if (gaveUp) throw giveUpError();
      const j = await Promise.race([window.api.call('get_job', { job_id: r.job_id }), stopped.then(() => null)]);
      if (gaveUp || !j) throw giveUpError();
      last = j;
      busy.update(j.progress, j.stage_label || options.stage || null);
      if (mayAbandon) {
        // 取り消しを出さない待ち: 長くなったら抜ける道（待つのをやめる）だけを出す
        if (!offered && Date.now() - t0 >= abandonMs && j.status === 'running') offered = true;
        busy.cancelWith(offered && j.status === 'running' ? abandon : null,
          { button: '待つのをやめる（Esc）', note: '待つのをやめている…' });
      } else {
        busy.cancelWith(cancellable && j.status === 'running' && j.cancellable ? cancel : null);
      }
      if (j.status === 'done') return j;
      if (j.status === 'canceled') {
        const err = new EngineError(`${name}: 取り消した`);
        err.cancelled = true;
        throw err;
      }
      if (j.ok === false || j.status === 'error') throw new EngineError(`${name}: ${j.error}`, j);
      onProgress?.(((Date.now() - t0) / 1000).toFixed(0));
    }
  } finally {
    if (own) busy?.finish();
    else busy?.cancelWith(null);
  }
}

let analyzedHook = null;
/** analyze_take が済んだら呼ぶ関数（結果を受け取る。f0.js がピッチ検出の方式のチェックを曲の方式に合わせる）。 */
export function onAnalyzed(fn) { analyzedHook = fn; }
export function analyzed(r) { analyzedHook?.(r); }

/** トラックの解析（analyze_take。画面は常にジョブで頼む）。キャッシュで済めばその場で返り、何も出さない（issue #63）。
 * ジョブになったら（準備が終わっていない・重い計算が残る）止める処理のポップアップを出す。
 * hold（`laterBusy()`）を渡すと、終わった後もそのまま（描き直してから呼び出し側が finish する）。
 * 渡さなければ label・target で作り、ここで終わらせる。cancel: false で取り消しを出さない。 */
export async function analyzeTake({ hold = null, label = 'トラックを準備している', target = '', cancel = false,
  onProgress = null, background = true } = {}) {
  const h = hold || laterBusy({ label, target });
  try {
    const r = await callJob('analyze_take', { background }, onProgress,
      { busy: h, stage: 'トラックの解析', cancel });
    S.awaitPrep = null;              // 解析が済んだ: 待つのをやめて空にしていた表示を描いてよい
    analyzed(r);
    return r;
  } catch (err) {
    if (err.abandoned) await abandonHook?.(err);
    throw err;
  } finally {
    if (!hold) h.finish();
  }
}

/** 画面向けの描画データ。配列は JSON ファイルの中にあるので main に読んでもらう。 */
export async function viewData(range) {
  const r = await call('export_view_data', range || {});
  const vd = await window.api.readJson(r.path);
  // The view file name also includes export range arguments; the unqualified
  // revision is returned explicitly by both export_view_data and render_audition.
  vd.view_rev = r.view_rev || null;
  // 曲の取り消しの履歴（セッション。issue #16）は結果に付いてくる。プロジェクトの changeset の一覧はそのまま
  if (r.history) vd.history = { ...(vd.history || {}), ...r.history };
  // 書き出しの既定のパスは結果の値が今のもの（描画データは前に作ったものを使い回すことがある。issue #63）
  if (r.export_default_path !== undefined) vd.export_default_path = r.export_default_path;
  return vd;
}
