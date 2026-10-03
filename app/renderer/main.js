// 起動と配線。
//
// 起動時: プロジェクトを開く（起動引数か前回のプロジェクト。無ければ空のまま）
//   → load_project / new_project / open_project（旧形式）→ analyze_take（長いときはジョブなので get_job をポーリング）
//   → export_view_data → 描画。
// ファイル > 新規プロジェクト・開く・保存・名前を付けて保存（issue #33）もここ。
import { analyzeTake, call, callJob, onDocument, status, $ } from './engine.js';
import { beginBusy, busyState, installBusy, laterBusy, refuseWhileBlocking } from './busy.js';
import {
  S, adopt, bandOf, clearProject, defaultPitchView, editedCurve, phSpan, pitchOf, planWarp, spanOf, strokeData, totalSec,
  toEdited, utterances,
} from './state.js';
import {
  attach, corrText, edStep, edgeInfo, fadeInfo, focusRange, hasConnFocus, nearPair, render, renderToolbar,
} from './draw.js';
import {
  G as GRID, barsMode, loadGrid, snapTime, tempo as curTempo, ticks,
} from './grid.js';
import { closeTempoPop, installTempo, openTempoPop, tempoEditing } from './tempo.js';
import { install, jumpToUtterance, openLyrics, openTr, setTool } from './interact.js';
import {
  awaiting, enqueue, exportWav, handleEngineError, idle, lastPending, loadLyricsText, loadLyricsScore, redo, refresh, resetOriginal,
  setLyrics, undo, waitFor, wake,
} from './edits.js';
import {
  audioMuted, clearPreviewLog, invalidate, playState, previewEnabled, previewLogOf, previewState, setPreviewEnabled,
} from './audio.js';
import { COMMANDS, WHEEL, runCommand, setHost, installKeys, syncAppMenu, appMenuTemplate, flushAppMenu } from './commands.js';
import { installTitlebar, menubarState } from './titlebar.js';
import { bandColor, lineColor, pitchCorr, timingCorr } from './corr.js';
import { installMenus, menuItems } from './menus.js';
import {
  acceptCandidate, asrInfo, asrSettled, dismissCandidate, installAsr, refreshAsr, transcribeAt,
} from './asr.js';
import { installKeysDialog, keysOpen, openKeys, closeKeys } from './keysdlg.js';
import { closeUpdates, installUpdates, openUpdates, updatesOpen, updatesState } from './updates.js';
import { aiOpen, closeAi, installAiDialog, openAi } from './aidlg.js';
import { askText } from './askai.js';
import { commandFor, keysOf, loadOverrides, overrides } from './keys.js';
import { installFirstRun, setModelSizes, showFirstRun } from './first-run.js';
import { f0State, onF0Change, refreshF0 } from './f0.js';
import { addonsOpen, addonsState, closeAddons, installAddons, openAddons } from './addons.js';
import { adoptDoc, adoptSession, guideSuffix, loadSession, onDoc, phonemeSuffix, setTrack } from './session.js';
import {
  addTrackFile, installTracks, isDragging, removeTrack, renameTrack, renderTracks, selectTrack, setGuide,
  setKind, setTrackHeight, setViewTimeline, soundRegion, TRACK_H, trackHeight, tracksState, tvT, tvX,
  commitOrder, panTracks, setTracksView, tracksView, zoomTracks,
} from './tracks.js';

const root = $('#mock');
const svg = $('#roll');
let booted = false;
let savedView = null;     // 前回の表示範囲（**どのテイクのものか**まで覚える）
let openingBusy = null;
let menuOpenPending = false;

function saveView() {
  clearTimeout(saveView._t);
  saveView._t = setTimeout(() => {
    savedView = { t0: S.view.t0, span: S.view.span, take: S.take?.path || null,
      pv: S.pv ? { ...S.pv } : null, trackH: trackHeight(), tv: tracksView() };
    window.api.saveState({ view: savedView });
  }, 400);
}

async function openProject(take, guide, opts = {}) {
  return opening(() => openProjectNow(take, guide, opts));
}

/** 開いている間（open_project → 解析。長いと 10 秒）は、ドロップ・書き出しを受けない
 * （S.busy は編集の確定用で、ここでは立たない。2 本目の open_project が並んで走らないように）。
 * 始まった瞬間から止める処理（busy.js の覆い）で入力を止め、0.2 秒を超えたらポップアップを出す。 */
async function opening(fn, fromMenu = false) {
  if (S.opening || (menuOpenPending && !fromMenu)) return false;
  S.opening = 1;
  S.awaitPrep = null;                 // 別の曲を開く: 前の曲の準備を待つのをやめていた表示は捨てる
  try {
    // 開き直しは、順番待ちの編集が全部当たってから（離した直後の編集を前の曲に当て損ねない。issue #16）
    if (!idle()) {
      status('前の編集を当ててから開きます');
      await waitFor(() => idle() && !S.drag);
    }
    const busy = beginBusy({ label: 'プロジェクトを開いている', modal: true });
    openingBusy = busy;
    try { return await fn(); }
    catch (err) {
      if (!err.cancelled) throw err;
      clearProject();
      render();
      renderTracks();
      renderToolbar();
      status('開く処理を取り消した。トラックを選ぶと解析を再開できます');
      return false;
    } finally {
      busy.finish();
      if (openingBusy === busy) openingBusy = null;
    }
  } finally {
    S.opening = 0;
  }
}

const baseName = (p) => String(p || '').split(/[\\/]/).pop();
const AUDIO_FILE = /\.(wav|wave|bwf|flac|aiff?)$/i;

/** 旧形式で開く（テイクの WAV → projects/…。テストの --take --project-dir・ガイドを開く）。 */
async function openProjectNow(take, guide, { keepView = true, lyrics = null,
  guideLyrics = null, projectDir = null, restoreTrack = false } = {}) {
  status(`開いている… ${baseName(take)}`);
  openingBusy?.set({ target: baseName(take) });
  const op = await call('open_project', {
    take_path: take, guide_path: guide || null, project_dir: projectDir || null,
    lyrics: lyrics || null, guide_lyrics: guideLyrics || null, author: 'human',
  });
  return afterOpen(op, { keepView, restoreTrack, take });
}

/** 新規プロジェクト（Ctrl+N）。take を渡すと最初のトラックにして開く（ドロップ・開くで音声を選んだ・起動引数）。
 * そのテイクの旧形式のプロジェクトがあれば、エンジンがそちらを開く（前の編集がそのまま出る）。 */
async function newProjectNow({ take = null, guide = null, keepView = false } = {}) {
  status(take ? `開いている… ${baseName(take)}` : '新しいプロジェクトを作っている…');
  openingBusy?.set(take ? { target: baseName(take) } : { label: '新しいプロジェクトを作っている' });
  const op = await call('new_project', { take_path: take, guide_path: guide, author: 'human' });
  return afterOpen(op, { keepView, restoreTrack: op.opened === 'legacy', take });
}

/** プロジェクトを開く（.gliss・旧形式のディレクトリ／session.json・無題の作業場所・音声）。 */
async function loadProjectNow(path, { keepView = false } = {}) {
  if (AUDIO_FILE.test(path)) return newProjectNow({ take: path, keepView });
  status(`開いている… ${baseName(path)}`);
  openingBusy?.set({ target: baseName(path) });
  const op = await call('load_project', { path, author: 'human' });
  return afterOpen(op, { keepView });
}

/** 開いたプロジェクトの覚え書き（前回のプロジェクト・最近使ったプロジェクト）。 */
function rememberDoc(op) {
  const d = op.document;
  if (!d) return;
  // 前回のプロジェクト（起動時に開く）。旧形式のテイク（lastTake）は旧形式のときだけ覚える
  if (d.kind === 'gliss') {
    window.api.saveState({ lastDoc: { kind: 'gliss', path: d.path }, lastTake: null });
    window.api.pushRecent({ path: d.path });
  } else if (d.kind === 'untitled') {
    window.api.saveState({ lastDoc: { kind: 'untitled', work: d.work_dir }, lastTake: null });
  } else {
    // 旧形式: 今までどおりテイクのパスで覚える（最初のテイク = セッションのディレクトリのプロジェクト）
    const norm = (p) => String(p || '').replace(/\//g, '\\').toLowerCase();
    const prim = (op.session?.tracks || []).find((t) => norm(t.project_dir) === norm(op.session?.dir));
    const take = prim?.path || op.take?.path;
    if (take) {
      window.api.saveState({ lastDoc: null, lastTake: take, lastGuide: null });
      window.api.pushRecent({ take, guide: null });   // ガイドはセッションが覚えている
    }
  }
}

/** 開いた・作った後: セッションとプロジェクトの状態を取り込み、編集対象があれば解析して出す。 */
async function afterOpen(op0, { keepView = false, restoreTrack = false, take = null } = {}) {
  let op = op0;
  // 起動時・旧形式を開いたときは、前回編集していたトラックに戻す（open_project はテイクのトラックを編集対象にする）
  const last = op.session?.last_current;
  if (restoreTrack && last && last !== op.session.current
    && op.session.tracks.some((t) => t.id === last && t.kind === 'vocal')) {
    try { op = { ...op0, ...(await call('select_track', { track_id: last })) }; } catch { /* 開けなければテイクのまま */ }
  }
  adoptSession(op.session);
  adoptDoc(op.document);
  showFirstRun(false);
  rememberDoc(op);
  invalidate();
  const note = docNote(op0);
  if (!op.project_dir) {
    // 編集対象が無い（新規の空のプロジェクト・伴奏だけ）: 下のエディターは空
    clearProject();
    render();
    renderTracks();
    renderToolbar();
    booted = true;
    status((S.tracks.length ? 'ボーカルのトラックが無い（伴奏だけ）。' : '新しいプロジェクト: ')
      + 'WAV をウィンドウにドロップするか、ファイル > トラックを追加（Ctrl+Shift+O）' + note);
    return;
  }
  status(op.analyzed ? '解析済み。読み込んでいる…'
    : '解析している…（F0 → 音符 → ガイドとの対応付け → 音素）');
  try {
    await callJob('analyze_take', { background: true }, (sec) => status(`解析している… ${sec} 秒`),
      { busy: openingBusy, modal: true, label: 'プロジェクトを開いている', stage: 'トラックの解析' });
  } catch (err) {
    // 解析モデル（RMVPE）が未取得: 落とさず、取得の画面へ案内する（取得したあと「開く…」で開き直す）
    if (!err.cancelled && await analysisModelMissing()) {
      showFirstRun(true);
      throw new Error('解析モデルが未取得です。「ダウンロード」で取得してから、もう一度開いてください');
    }
    throw err;
  }
  // 前回の表示範囲は**同じテイクのときだけ**引き継ぐ。別の素材に使い回すと
  // 158 秒の曲を開いたのに 3 秒の窓が出る（逆もある）。
  // 前回戻したトラック（restoreTrack）のファイルと比べる
  const mine = keepView && savedView && savedView.span > 0 && savedView.take === (op.take?.path || take);
  if (mine) S.view = { t0: savedView.t0, span: savedView.span };
  S.pv = mine && savedView.pv ? { ...savedView.pv } : null;     // 縦（音程）の表示も同じテイクのときだけ
  if (mine && savedView.tv) setTracksView(savedView.tv);        // 上の横の表示範囲も（issue #39）
  await refresh({ keepView: !!mine });
  if (!mine) focusFirstUtterance();
  booted = true;
  const name = S.take?.name || '';
  const g = guideSuffix();
  const ph = S.ph ? ` / 音素 ${S.ph.phonemes.filter((p) => p.label !== 'silence').length}` : '';
  status(`${name}${g} — ノート ${S.pitched.length}${ph}${phonemeSuffix()}${note}`);
}

/** 解析に要る重み（RMVPE）がまだ無いか（エンジンに聞けなければ「無い」とは言わない）。 */
async function analysisModelMissing() {
  try {
    const info = await call('engine_info', {});
    return info.ok !== false && !info.rmvpe_model_found;
  } catch { return false; }
}

/** 開いたときに知らせること（保存していない変更の続き・見つからない音声・外で書き換わっていた）。 */
function docNote(op) {
  const out = [];
  if (op.note) out.push(op.note);
  if (op.recovered) out.push('保存していない変更の続きから開いた');
  if (op.backup) out.push(`ファイルが外で書き換わっていたので、前の作業を退避した: ${op.backup}`);
  if (op.missing?.length) out.push(`見つからない音声 ${op.missing.length} 個: ${op.missing.map(baseName).join('、')}`);
  return out.length ? `（${out.join('。')}）` : '';
}

// ---------------------------------------------------------------- 保存（issue #33）
/** 名前を付けて保存の既定: 今のファイル、無ければ最初のトラックの音声の隣に「名前.gliss」。 */
function defaultSavePath() {
  if (S.doc?.path) return S.doc.path;
  const first = S.tracks.find((t) => t.kind === 'vocal') || S.tracks[0];
  const name = (S.doc?.kind === 'untitled' ? (first?.name || S.doc?.name) : S.doc?.name) || '無題';
  const dir = first?.path ? first.path.replace(/[\\/][^\\/]*$/, '') : '';
  return dir ? `${dir}\\${name}.gliss` : `${name}.gliss`;
}

/** 保存（Ctrl+S）・名前を付けて保存（Ctrl+Shift+S）。無題・旧形式の保存は名前を付けて保存になる。保存したら true。
 *
 * **保存と、競合したときの読み直しは編集と同じ順番待ちの中で行う**（終わるまで次の操作を始めない）。
 * 保存が競合したら、保存が始まった後に入った操作も含めて、順番待ちの操作を捨てる（edits.js の handleEngineError）。 */
export async function saveDoc({ as = false } = {}) {
  if (!S.doc) { status('保存するプロジェクトが無い'); return false; }
  if (!idle()) {
    status('前の編集を当ててから保存します');
    await waitFor(() => idle() && !S.drag);
  }
  let path = null;
  if (as || S.doc.kind !== 'gliss') {
    path = await window.api.saveProjectDialog(defaultSavePath());
    if (!path) { status('保存をやめた'); return false; }
  }
  return (await enqueue(() => saveNow(path), { waitMsg: '前の編集を当ててから保存します' })) === true;
}

async function saveNow(path) {
  S.busy = true;
  renderToolbar();
  try {
    const r = await call('save_project', path ? { path } : {});
    adoptSession(r.session);
    adoptDoc(r.document);
    rememberDoc(r);
    if (r.moved && r.project_dir) {
      // 作業場所が変わった（無題・旧形式・別名で保存）: 同じトラックを開き直したので描き直す
      // （準備済みならその場で返り、何も出さない。解析が要るときだけ止める処理のポップアップ）
      const hold = laterBusy({ label: `${S.take?.name || 'トラック'} を読み直している`, target: baseName(r.saved) });
      try {
        await analyzeTake({ hold, onProgress: (sec) => status(`読み込んでいる… ${sec} 秒`) });
        await refresh({ keepView: true });
      } catch (err) {
        if (!err.abandoned) throw err;
        // 保存はできている。読み直しの待ちをやめただけ（表示は空のまま、準備が終わったら描き直す）
        await handleEngineError(err);
        status(`保存した: ${r.saved}。${$('#status')?.textContent || ''}`);
        return true;
      } finally {
        hold.finish();
      }
    }
    status(`保存した: ${r.saved}`);
    return true;
  } catch (err) {
    if (!await handleEngineError(err)) status(`保存できなかった: ${err.message}`);
    return false;
  } finally {
    S.busy = false;
    renderToolbar();
    wake();
  }
}

/** 今のプロジェクトを閉じてよいか（新規・開く・閉じるの前）。保存していない変更があれば「保存しますか」。 */
export async function confirmDiscard() {
  if (!idle()) await waitFor(() => idle() && !S.drag);
  if (!S.doc?.dirty) return true;
  const a = await window.api.askSave(S.doc.name);
  if (a === 'cancel') { status('やめた'); return false; }
  if (a === 'save') return saveDoc();
  await call('close_project', { discard: true });
  adoptDoc(null);
  return true;
}

/** 曲全体（158 秒）のうち歌っているのが一部だけのときは、**最初の発声に寄せる**。
 *
 * 全体表示のままだと 141 秒の無音がピアノロールを占領して何も読めない
 * （設計の方針「情報量を最小に」は「無音を大きく描く」ことではない）。 */
export function focusFirstUtterance() {
  const us = utterances();
  if (!us.length) return false;
  const voiced = us.reduce((a, [s0, s1]) => a + (s1 - s0), 0);
  const total = totalSec();
  if (voiced > total * 0.5) return false;          // ほぼ全部歌っているなら触らない
  const last = us[us.length - 1];
  focusRange(toEdited(us[0][0]), toEdited(Math.min(last[1], us[0][1] + 12)));
  S.head = toEdited(us[0][0]) + S.off;
  return true;
}

/** 表示 > ズームを戻す: 縦（音程）・横・トラックの高さ・上の横の表示範囲（全体表示）を、開いたときの表示に戻す。 */
export function zoomReset() {
  setTracksView(null);
  setTrackHeight(TRACK_H.DEF);
  if (!S.vd) return;                 // 編集対象が無い: 前回の表示（別のテイクのもの）は上書きしない
  S.pv = defaultPitchView();
  S.view = { t0: 0, span: totalSec() };
  focusFirstUtterance();
  render();
  saveView();
}

/** メニューバー（ファイル／編集／ノート／表示）から来た指示（main の Menu の click → `menu` の IPC）。
 * ファイルの操作はここで、それ以外はコマンドの表（commands.js）で実行する。 */
const FILE_CMDS = new Set(['new-project', 'open-take', 'save', 'save-as', 'open-guide', 'add-track', 'open-recent',
  'load-lyrics', 'import-lyrics', 'export', 'export-as']);
async function onMenu({ cmd, arg }) {
  // 止める処理（開く・書き出し・準備の待ち）の間は受けない（覆いの外から来る。ポップアップを揺らして伝える）
  if (refuseWhileBlocking()) return false;
  const reservesOpen = ['new-project', 'open-take', 'open-recent', 'add-track', 'open-guide'].includes(cmd);
  if ((S.opening || menuOpenPending) && reservesOpen) {
    status('開いている途中。終わってから操作してください');
    return false;
  }
  if (reservesOpen) menuOpenPending = true;
  if (!FILE_CMDS.has(cmd)) {
    try {
      return await runCommand(cmd);
    } catch (err) {
      status(`${cmd} に失敗: ${err.message}`);
      return null;
    }
  }
  try {
    if (cmd === 'new-project') {
      if (await confirmDiscard()) await opening(() => newProjectNow(), true);
    } else if (cmd === 'open-take') {
      // 開く…: .gliss・旧形式のプロジェクト・音声（音声は新しいプロジェクトのトラックに）
      const p = await window.api.pickProject();
      if (p && await confirmDiscard()) await opening(() => loadProjectNow(p), true);
    } else if (cmd === 'save') {
      return await saveDoc();
    } else if (cmd === 'save-as') {
      return await saveDoc({ as: true });
    } else if (cmd === 'open-guide') {
      // ガイドのトラックとして足す（もうトラックにあればガイドに指定するだけ）。編集中のテイクはそのまま
      const guide = await window.api.pickFile('guide');
      if (guide && S.tracks.length) await addTrackFile(guide, { guide: true });
      else if (guide && S.take?.path) await opening(
        () => openProjectNow(S.take.path, guide, { keepView: true }), true);
      else if (guide) status('ガイドの前にテイクを開くこと（WAV を落とすか、トラックを追加）');
    } else if (cmd === 'add-track') {
      // トラックとして足す（種類はファイル名から。編集対象は変えない）。何も開いていなければテイクとして開く
      const p = await window.api.pickFile('track');
      if (p && (S.tracks.length || S.doc)) await addTrackFile(p, { select: !S.session?.current });
      else if (p) await opening(() => newProjectNow({ take: p }), true);
    } else if (cmd === 'open-recent') {
      // .gliss（path）か旧形式のテイク（take）。旧形式はエンジンが projects/… か、名前を付けて保存した .gliss を開く
      if (!(arg?.path || arg?.take) || !(await confirmDiscard())) return null;
      if (arg.path) await opening(() => loadProjectNow(arg.path), true);
      else await opening(() => newProjectNow({ take: arg.take }), true);
    } else if (cmd === 'load-lyrics') {
      const f = await window.api.openLyricsFile();
      if (f) await loadLyricsText(f.text);
    } else if (cmd === 'import-lyrics') {
      const path = await window.api.pickLyricsScore();
      if (path) {
        const info = await call('inspect_lyrics_score', { path });
        const tracks = info.tracks.filter((t) => t.lyric_notes > 0);
        if (!tracks.length) { status('譜面に歌詞付きのトラックがない'); return null; }
        const track = tracks.length === 1 ? tracks[0].index : await window.api.chooseLyricsTrack(tracks);
        if (track == null) return null;
        const r = await loadLyricsScore(path, track);
        if (r) status(`歌詞 ${r.n_entries} 区間を読み込んだ。対応できない区間 ${r.unmatched.length} 件`);
      }
    } else if (cmd === 'export') {
      return await exportWav();
    } else if (cmd === 'export-as') {
      return await exportWav({ ask: true });
    }
  } catch (err) {
    status(`${cmd} に失敗: ${err.message}`);
  } finally {
    if (reservesOpen) menuOpenPending = false;
  }
}

/** 外部（Claude Code）が project.json を書き換えた: 読み直す。
 *
 * **ドラッグ中・前の編集の確定待ちの間は読み直さない**（終わってから読み直す）。読み直すと
 * ドラッグ中のプレビュー（S.local）が消え、離したときの量が失われる。確定した後の view data には
 * 外部の変更も入っているので、待っても取りこぼさない。 */
let reloadWaiting = false;
async function reloadExternal() {
  if (reloadWaiting) return;
  reloadWaiting = true;
  try {
    await waitFor(() => idle() && !S.drag);
  } finally {
    reloadWaiting = false;
  }
  // 取り消しの履歴はエンジンが持つ（Claude Code の編集も同じ履歴に入っている）。描き直せば要約も新しくなる
  invalidate();
  if (awaiting()) return;             // 準備を待つのをやめて空にしている: 準備が終わったら描き直す
  try {
    const before = JSON.stringify([S.vd?.edits, S.vd?.history, S.vd?.lyrics]);
    await refresh();
    // 競合の読み直しと同じファイル通知が後から届いても、操作を当てなかった案内を消さない。
    const unchanged = before === JSON.stringify([S.vd?.edits, S.vd?.history, S.vd?.lyrics]);
    if (!unchanged || !/今の操作は(?:当てていない|一部だけ当たった)/.test($('#status')?.textContent || '')) {
      status('外部の変更を読み込んだ');
    }
  } catch (err) {
    status(`再読込に失敗: ${err.message}`);
  }
}

/** 外部（Claude Code）が session.json（トラック）を書き換えた: 一覧を読み直す。
 *
 * ガイドの指定・位置が変わって編集中のプロジェクトのガイドが古くなったら（guide_stale）、
 * 同じトラックを開き直して解析（ガイドの対応付け）し、描き直す。 */
let sessionWaiting = false;
async function reloadSessionExternal() {
  if (sessionWaiting) return;
  sessionWaiting = true;
  try {
    await waitFor(() => idle() && !S.drag && !S.opening && !isDragging());
  } finally {
    sessionWaiting = false;
  }
  // 順番待ちに入れて、読み直しの間（開き直し・解析）は次の編集を当てない
  await enqueue(async () => {
    S.busy = true;
    try {
      const r = await loadSession();
      if (r.guide_stale && r.current) {
        S.opening = (S.opening || 0) + 1;
        try {
          const op = await call('select_track', { track_id: r.current }, { busy: false });
          adoptSession(op.session);
          const hold = laterBusy({ label: '外部の変更を読み直している', target: S.take?.name || '' });
          try {
            await analyzeTake({ hold, onProgress: (sec) => status(`解析している… ${sec} 秒`) });
            await refresh();
          } finally {
            hold.finish();
          }
        } finally {
          S.opening -= 1;
        }
      }
      status('外部の変更（トラック）を読み込んだ');
    } catch (err) {
      if (!await handleEngineError(err)) status(`トラックの再読込に失敗: ${err.message}`);
    } finally {
      S.busy = false;
      render();
    }
  });
}

// ---------------------------------------------------------------- ドラッグ＆ドロップで開く
// DAW（のイベントを「ファイルの場所を開く」で出したエクスプローラ）やエクスプローラから WAV を
// ウィンドウに落とすと**テイク**として開く（今のガイドはそのまま）。**Shift を押しながら**落とすと
// **ガイド**として重ねる（テイクはそのまま・編集リストも残る）。画面の要素は増やさない:
// 落とす前の案内は下のステータス行に出す（DAW のドロップと同じく、落とす場所ではなく修飾キーで分ける）。
const AUDIO_EXT = /\.(wav|wave|bwf|flac|aiff?)$/i;
let dropHint = null;          // ドラッグ中に出していた案内（離れたら元のステータスに戻す）

function droppedPaths(dt) {
  const out = [];
  for (const f of dt?.files || []) {
    const p = window.api.pathForFile(f);
    if (p) out.push(p);
  }
  return out;
}

function hasFiles(dt) {
  return !!dt && [...(dt.types || [])].includes('Files');
}

function showDropHint(e) {
  const guide = e.shiftKey;
  const tracks = S.tracks.length > 0 || !!S.doc;
  const text = (S.opening || S.openQueued || menuOpenPending) ? '開いている途中。終わってから落とすこと'
    : guide
      ? (S.take ? `離すとガイドとして重ねる${tracks ? '（トラックに足す）' : ''}` : 'ガイドの前にテイクを開くこと')
      : tracks
        ? '離すとトラックに足してテイクとして開く（伴奏は足すだけ。Shift を押しながら: ガイドとして重ねる）'
        : '離すと新しいプロジェクトのテイクとして開く（Shift を押しながら: ガイドとして重ねる）';
  if (dropHint === null) dropHint = $('#status').textContent;
  status(text);
}

function clearDropHint() {
  if (dropHint !== null) status(dropHint);
  dropHint = null;
}

/** 落とされたファイルを開く（テストからも呼ぶ）。`guide`: Shift で落とした。 */
export async function openDropped(paths, { guide = false } = {}) {
  const audio = paths.filter((p) => AUDIO_EXT.test(p));
  if (!audio.length) {
    status(paths.length ? `音声ファイルではない: ${paths[0].split(/[\\/]/).pop()}` : '');
    return false;
  }
  if (S.opening || S.openQueued || menuOpenPending) {
    status('開いている途中。終わってからもう一度落とすこと');
    return false;
  }
  // 順番待ちの編集はテイクを開き直す前に当てる（トラックに足すときは順番待ちの後ろに並ぶ）
  const same = (a, b) => !!a && !!b && a.toLowerCase() === b.toLowerCase();
  const name = audio[0].split(/[\\/]/).pop();
  if (S.tracks.length || S.doc) return dropIntoSession(audio, guide, same, name);
  // 何も開いていない: 新しいプロジェクトの最初のトラックとして開く（issue #33。そのテイクの旧形式のプロジェクトが
  // あればエンジンがそちらを開く）。2 つ目からはトラックとして足す
  if (guide) { status('ガイドの前にテイクを開くこと'); return false; }
  const [first, ...rest] = audio;
  await opening(() => newProjectNow({ take: first }));
  for (const p of rest) if (!S.tracks.some((t) => same(t.path, p))) await addTrackFile(p);
  return true;
}

/** セッション（トラック）を開いているときのドロップ: **トラックとして足す**（issue #7 §5）。
 *
 * 1 つ目: ボーカルなら足して編集対象にする（DAW の録音ファイルを落として直す流れは今までと同じ）。
 *   もうトラックにあるファイルならそのトラックに切り替える。伴奏（ファイル名から推す）は足すだけ。
 *   Shift を押しながら: ガイドとして足す（もうあればガイドに指定するだけ）。編集中のテイクはそのまま。
 * 2 つ目から: トラックとして足すだけ（編集対象は変えない）。 */
async function dropIntoSession(audio, guide, same, name) {
  const hit = (p) => S.tracks.find((t) => same(t.path, p));
  const [first, ...rest] = audio;
  const t = hit(first);
  const cur = S.session?.current;
  if (guide) {
    if (t && t.id === cur) { status(`テイクと同じファイル: ${name}`); return false; }
    if (t && t.guide) { status(`もうガイドとして開いている: ${name}`); return false; }
    await addTrackFile(first, { guide: true });
  } else if (t) {
    if (t.id === cur) { status(`もう開いている: ${name}`); return false; }
    if (t.kind !== 'vocal') { status(`伴奏のトラック（${t.name}）は編集しない`); return false; }
    await selectTrack(t.id, { first: true });
  } else {
    await addTrackFile(first, { select: true });   // 伴奏（名前から）は選ばれない
  }
  for (const p of rest) if (!hit(p)) await addTrackFile(p);
  return true;
}

function installDrop(el) {
  // 既定の動き（ファイルへ移動して画面が消える）を止める。受け取るのはウィンドウ全体
  // ファイル以外（歌詞欄へのテキストのドラッグなど）は既定の動きのまま
  window.addEventListener('dragover', (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    $('#firstRunDrop').classList.add('dragging');
    showDropHint(e);
  });
  window.addEventListener('dragleave', (e) => {
    // 子要素の間を移るたびに来るので、ウィンドウの外に出たときだけ戻す
    if (!e.relatedTarget) { clearDropHint(); $('#firstRunDrop').classList.remove('dragging'); }
  });
  window.addEventListener('drop', async (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    if (refuseWhileBlocking()) { clearDropHint(); $('#firstRunDrop').classList.remove('dragging'); return; }
    const paths = droppedPaths(e.dataTransfer);
    const guide = e.shiftKey;
    clearDropHint();
    $('#firstRunDrop').classList.remove('dragging');
    if (!paths.length) return;
    try {
      await openDropped(paths, { guide });
    } catch (err) {
      status(`開けなかった: ${err.message}`);
    }
    el.focus();
  });
}

async function boot() {
  attach(svg);
  installDrop(root);
  installFirstRun({ onOpen: () => onMenu({ cmd: 'open-take' }), onAi: (cmd) => onMenu({ cmd }) });
  setHost({ menu: onMenu, saveView, openKeys, zoomReset, openAi, openUpdates, openAddons });
  installMenus(root);
  installKeysDialog(root);
  installAiDialog(root);
  // 自動更新（ヘルプ > 更新を確認…）。再起動して更新の前に「保存しますか」を通す
  installUpdates(root, { confirmDiscard });
  // 任意機能のアドオン（初回画面の行・ヘルプ > モデルと追加の機能…）
  installAddons(root);
  const dialogOpen = () => keysOpen() || aiOpen() || updatesOpen() || addonsOpen();
  installKeys({ dialogOpen });
  installAsr({ dialogOpen });   // 聞き取りの候補の Enter / Esc（issue #54）
  // ダイアログを開いている間は Alt でメニューバーへ移らない
  installTitlebar({ beforeOpen: flushAppMenu, blocked: dialogOpen, keyTaken: (k) => !!commandFor(k) });
  install(svg, root, saveView);
  installTracks(root, { onViewChanged: saveView, onNewTake: focusFirstUtterance });
  installTempo(root);
  new ResizeObserver(() => render()).observe(svg);
  window.api.onMenu(onMenu);
  window.api.onProjectChanged(reloadExternal);
  window.api.onSessionChanged(reloadSessionExternal);
  // プロジェクトのファイル（issue #33）: ツールの返り値の document を取り込む・閉じる前の「保存しますか」
  onDocument(adoptDoc);
  onDoc(() => { syncAppMenu(); showFirstRun(!S.doc && !S.tracks.length); });
  let closing = false;           // 「保存しますか」を出している間にもう一度閉じようとしても、2 つ目は出さない
  window.api.onConfirmClose(async () => {
    if (closing) return;
    closing = true;
    try {
      if (await confirmDiscard()) window.api.closeNow();
    } catch (err) {
      status(`閉じられなかった: ${err.message}`);
    } finally {
      closing = false;
    }
  });

  const b = await window.api.bootstrap();
  setModelSizes(b.modelSizes);
  loadOverrides(b.keys);          // キーボードショートカットの設定（ユーザー設定。issue #22）
  loadGrid(b.grid);               // スナップのオン・オフとグリッドの細かさ（ユーザー設定。issue #18）
  setPreviewEnabled(b.preview !== false, { save: false });   // つかんだノートを鳴らす（ユーザー設定。issue #27）
  renderToolbar();                // ツールチップとメニューバーの表記を設定に合わせる
  syncAppMenu();
  savedView = (b.view && b.view.span > 0) ? b.view : null;   // 前回のズーム・スクロール位置
  if (savedView?.trackH) setTrackHeight(savedView.trackH);
  if (b.engineError) {
    status('エンジンに接続できていない。ログを確認すること。');
    renderToolbar();
    return;
  }
  onF0Change(syncAppMenu);
  await refreshF0();              // ピッチ検出の方式（エンジンが実際に使うもの・RMVPE の重みの有無）をメニューのチェックへ
  const { take, guide } = b;
  try {
    if (b.project) {
      // .gliss・無題の作業場所（前回のプロジェクト・--project）
      await opening(() => loadProjectNow(b.project, { keepView: true }));
    } else if (take && (b.projectDir || b.lyrics)) {
      // テスト（--take --project-dir）: 旧形式のまま、置き場を指定して開く
      await openProject(take, guide,
        { keepView: true, lyrics: b.lyrics, guideLyrics: b.guideLyrics,
          projectDir: b.projectDir, restoreTrack: !b.fromArgs });
    } else if (take) {
      // 起動引数の WAV（gliss.cmd・DAW の外部エディタ）・前回の旧形式のテイク: 新しいプロジェクトのトラックとして
      // 開く（旧形式のプロジェクトがあればそちらを開く）
      await opening(() => newProjectNow({ take, guide, keepView: true }));
    } else {
      showFirstRun(true);
      status('');
    }
  } catch (err) {
    if (!S.doc && !S.tracks.length) showFirstRun(true);
    status(`開けなかった: ${err.message}`);
    console.error(err);
  }
  renderToolbar();
}

// テストから触る口（Playwright はこれを使う）
window.__app = {
  S, render, refresh, openProject, openDropped, totalSec, openLyrics, onMenu,
  // プロジェクトのファイル（issue #33）
  newProject: (o) => opening(() => newProjectNow(o || {})), loadProject: (p) => opening(() => loadProjectNow(p)),
  saveDoc, confirmDiscard, doc: () => (S.doc ? { ...S.doc } : null), title: () => document.title,
  // トラック（issue #7）
  playState, audioMuted, setTrack, loadSession, tracks: () => S.tracks.map((t) => ({ ...t })),
  tracksState, selectTrack, setGuide, setViewTimeline, soundRegion, renderTracks, tvX, tvT,
  addTrackFile, removeTrack, setKind, renameTrack,
  // トラックの並び順（issue #38）・上の横の表示範囲（issue #39）
  commitOrder, zoomTracks, panTracks, tracksView, setTracksView,
  // 右クリックのメニューとコマンド（issue #17）・キーボードショートカットの設定（issue #22）
  runCommand, menuItems, openKeys, closeKeys, keysOpen, appMenuTemplate,
  // AI とつなぐ（ヘルプ > AI とつなぐ…）・右クリックの「AI に頼む」の文・AI の編集の印（ノートの縁）
  openAi, closeAi, aiOpen, askText, aiNotes: () => [...S.aiNotes],
  // 自動更新（ヘルプ > 更新を確認…）
  openUpdates, closeUpdates, updates: updatesState,
  openAddons, closeAddons, addons: addonsState,
  // タイトルバーのメニューバー（main の Menu の写し・開いている段）
  menubar: menubarState,
  // 処理中の表示（issue #63 の 4）: 止める処理の覆いとポップアップ・線
  busy: busyState,
  // 聞き取り（区間の音声認識。issue #54）
  asr: {
    info: () => asrInfo(), settled: asrSettled, refresh: refreshAsr, transcribeAt, acceptCandidate,
    dismissCandidate,
    candidate: () => (S.asrCand ? JSON.parse(JSON.stringify(S.asrCand)) : null),
    running: () => (S.asrRunning ? { ...S.asrRunning } : null),
  },
  commands: () => COMMANDS.map((c) => ({ id: c.id, label: c.label, group: c.group, keys: keysOf(c.id) })),
  keyOverrides: () => overrides(),
  // ピッチ検出の方式（編集 > ピッチ検出の方式）
  f0: f0State,
  // ホイールの割り当て・つかんだノートのプレビュー音（issue #27）。音は出さずに「鳴らそうとしたもの」を見る
  wheels: () => WHEEL.map((w) => ({ id: w.id, label: w.label, keys: keysOf(w.id) })),
  previewState, previewLog: previewLogOf, clearPreviewLog, previewEnabled, setPreviewEnabled,
  /** 取り消し（issue #16）: まだ当たっていない操作・エンジンの履歴の要約・いま出している「元に戻す: ○○」 */
  pending: () => S.pending.filter((it) => !it.started && !it.canceled).map((it) => it.label),
  hist: () => (S.hist ? JSON.parse(JSON.stringify(S.hist)) : null),
  undoTitle: () => document.querySelector('#bUndo').title,
  redoTitle: () => document.querySelector('#bRedo').title,
  lastPending: () => lastPending()?.label || null,
  undo, redo, setLyrics,
  jumpToUtterance, focusFirstUtterance, utterances,
  view: () => ({ t0: S.view.t0, span: S.view.span }),
  lyricsEntries: () => S.lyricsEntries,
  ready: () => !!S.vd,
  /** 離した操作が全部当たり終わった（S.busy だけだと、計画の到着待ち・順番待ちの間を見落とす）。 */
  idle,
  notes: () => S.pitched.map((n) => ({
    id: n.id, start: n.start_sec, end: n.end_sec,
    editedStart: n.edited_start_sec, editedEnd: n.edited_end_sec,
    pitch: n.pitch_midi, edited: n.edited_pitch_midi, cents: n.cents, muted: !!n.muted,
    fadeIn: n.fade_in_sec || 0, fadeOut: n.fade_out_sec || 0,
  })),
  // グリッド・スナップ・テンポ（issue #18）
  grid: () => ({ ...GRID, bars: barsMode(), tempo: curTempo() ? { ...curTempo() } : null,
    step: edStep(), sel: document.querySelector('#gridDiv').value,
    options: [...document.querySelectorAll('#gridDiv option')].map((o) => o.value) }),
  setGrid: (patch) => { Object.assign(GRID, patch); render(); },
  snapTime, ticks, openTempoPop, closeTempoPop, tempoEditing,
  tempoText: () => ({ bpm: document.querySelector('#tBpm').textContent, sig: document.querySelector('#tSig').textContent,
    src: !document.querySelector('#tSrc').hidden, srcTitle: document.querySelector('#tSrc').title,
    none: document.querySelector('#tBpm').classList.contains('none') }),
  rulerLabels: (sel = '#roll') => [...document.querySelectorAll(`${sel} [data-rlab]`)].map((t) => t.textContent),
  gridLines: () => [...document.querySelectorAll('#roll [data-grid]')].map((l) => ({ x: +l.getAttribute('x1'), l: +l.dataset.grid })),
  // フェード（issue #20）
  fadeInfo,
  local: () => ({ fade: [...S.local.fade.entries()] }),
  phonemes: () => (S.ph?.phonemes || []).map((p) => ({
    id: p.id, text: p.text, kana: p.kana, label: p.label,
    start: p.edited_start_sec, end: p.edited_end_sec,
    len: p.edited_end_sec - p.edited_start_sec, stretchable: p.stretchable,
  })),
  boundaries: () => (S.ph?.boundaries || []).map((b) => ({
    id: b.id, index: b.index, sec: b.sec, edited: b.edited_sec, kind: b.kind,
    deviationMs: b.deviation_ms,
  })),
  /** ドラッグ中／プレビュー中の見かけの長さ（ローカル補間込み）。 */
  localLengths: () => (S.ph?.phonemes || []).map((p) => {
    const [a, b] = phSpan(p);
    return { id: p.id, label: p.label, len: b - a };
  }),
  /** いま画面に描いている blob（ドラッグ・スライダー中の見かけを含む）。
   * 「ドラッグ中の見た目 = 離した後の結果」を比べるのに使う。 */
  shapes: () => S.pitched.map((n) => {
    const [s0, s1] = spanOf(n);
    return { id: n.id, s: s0, e: s1, pitch: pitchOf(n), band: bandOf(n) };
  }),
  /** 補正の度合いと色（issue #37）: ドラッグ中の見かけを含む値と、エンジンが返した値。 */
  corr: () => S.notes.map((n) => ({
    id: n.id, timing: timingCorr(n), pitch: pitchCorr(n), band: bandColor(n), line: lineColor(n),
    engTiming: n.timing_corr || null, engPitch: n.pitch_corr || null,
  })),
  /** いま描いている色: 帯の塗り（ノートごと）・線の色（path ごと）・元の長さの印（選択中のノートだけ）。 */
  drawnColors: () => ({
    blobs: Object.fromEntries([...document.querySelectorAll('#roll path[data-blob]')].map((p) => [p.dataset.blob, p.getAttribute('fill')])),
    lines: [...document.querySelectorAll('#roll path[data-line]')].map((p) => p.getAttribute('stroke')),
    orig: [...document.querySelectorAll('#roll path[data-orig]')].map((p) => ({ id: p.dataset.orig, d: p.getAttribute('d'), stroke: p.getAttribute('stroke') })),
  }),
  /** 帯の高さ（エンジンの band_midi）と端のつかみ（v3 §3）。 */
  bands: () => S.pitched.map((n) => ({ id: n.id, band: n.band_midi, edited: n.edited_pitch_midi })),
  guideBands: () => (S.vd.guide_notes || []).map((g) => ({ id: g.id, band: g.band_midi, pitch: g.pitch_midi })),
  edgeInfo,
  zoomReset,
  pitchView: () => (S.pv ? { ...S.pv } : null),
  trackHeight,
  edgeHover: () => (S.edgeHover ? { ...S.edgeHover } : null),
  /** 編集前の秒 → 編集後の秒（確定済み）と、計画のプレビューでの行き先（テスト用）。 */
  toEdited,
  planWarp: (t) => planWarp(t),
  plan: () => (S.plan ? {
    id: S.plan.data.plan_id, kind: S.plan.data.kind, x: S.plan.x, pitch: S.plan.pitch,
    x0: S.plan.x0, pitch0: S.plan.pitch0,
    range: S.plan.data.x_range, snap: S.plan.data.snap_x,
    guides: [...S.plan.guides], takes: [...S.plan.takes], pitchPlan: S.plan.data.pitch,
    timing: S.plan.data.timing || [],
    notes: S.plan.data.notes || [], pairs: S.plan.data.pairs || [], info: S.plan.data.info || {},
  } : null),
  /** ガイドとの対応の表示（issue #53）: 描いている線・破線の丸・ホバーの説明。 */
  guideCorr: () => ({
    lines: [...document.querySelectorAll('#roll [data-corr-line]')].map((p) => ({
      key: p.dataset.corrLine, end: !!p.dataset.corrEnd, d: p.getAttribute('d'),
      stroke: p.getAttribute('stroke'), width: +p.getAttribute('stroke-width'),
    })),
    marks: [...document.querySelectorAll('#roll [data-corr-mark]')].map((c) => ({
      id: c.dataset.corrMark, cx: +c.getAttribute('cx'), dash: c.getAttribute('stroke-dasharray'),
      stroke: c.getAttribute('stroke'),
    })),
    tip: document.querySelector('#roll [data-corr-tip]')?.textContent || null,
  }),
  corrText,
  connections: () => S.pitched.map((n) => ({ id: n.id, prev: n.connected_prev ?? null,
    next: n.connected_next ?? null })),
  /** 接続の記号（B 案）: いま描いている境目と状態（connected / detached / cut）。 */
  glyphs: () => [...document.querySelectorAll('#roll [data-conn]')].map((g) => ({
    key: g.dataset.conn, state: g.dataset.state,
    dashed: !!g.querySelector('path[stroke-dasharray]'),
    path: g.querySelector('path')?.getAttribute('d') || null,
  })),
  hasConnFocus,
  /** 画面の px（#roll の左上から）→ 近い境目（テスト用。ページ座標で渡す）。 */
  nearPair: (x, y) => { const r = document.querySelector('#roll').getBoundingClientRect(); return nearPair(x - r.left, y - r.top); },
  guideOpacity: () => [...document.querySelectorAll('#roll [data-guide]')]
    .map((r) => ({ id: r.dataset.guide, op: +r.getAttribute('opacity') })),
  lyrics: () => S.lyrics,
  status: () => $('#status').textContent,
  // ツール・つなぎ・鉛筆
  tool: () => S.tool,
  setTool,
  openTr,
  /** いま画面に描いている黄色の曲線（フレームごとの MIDI。ドラッグ・スライダー・鉛筆のプレビュー込み）。 */
  editedCurve: () => editedCurve().slice(),
  /** エンジンが当てた曲線（view data そのまま）。 */
  engineCurve: () => S.vd.f0.take_edited_midi.slice(),
  origCurve: () => S.vd.f0.take_midi.slice(),
  f0Frame: () => ({ t0: S.vd.f0.t0_sec, hop: S.vd.f0.hop_sec }),
  transitions: () => (S.vd.transitions || []).map((t) => ({ ...t })),
  stroke: () => strokeData(),
  cutHover: () => (S.cutHover ? { ...S.cutHover } : null),
  allNotes: () => S.pitched.map((n) => ({ id: n.id, start: n.start_sec, end: n.end_sec })),
  /** 子音・息（音程の無いノート。issue #35）: 見かけの区間（ドラッグ中を含む）と確定した区間。 */
  noPitch: () => S.notes.filter((n) => n.kind !== 'note').map((n) => {
    const [s0, s1] = spanOf(n);
    return { id: n.id, kind: n.kind, start: n.start_sec, end: n.end_sec, s: s0, e: s1,
      editedStart: n.edited_start_sec, editedEnd: n.edited_end_sec,
      grab: !!document.querySelector(`#roll rect[data-nop="${n.id}"]`) };
  }),
};

installBusy();
boot();
