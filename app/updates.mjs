// 自動更新（electron-updater）の状態機械。Electron に依存しない（tests/updates.spec.js がフェイクの updater で確かめる）。
// 方針は docs/release-plan.md §4。main.mjs が electron-updater の autoUpdater を渡し、状態を画面（renderer/updates.js）へ送る。
//
// 状態（phase）:
//   unavailable  更新できない（開発版・app-update.yml が無い）。何もしない
//   idle         まだ確かめていない
//   checking     確かめている
//   current      新しい版は無い
//   available    新しい版がある（自動ダウンロードがオフのときは、ここで「ダウンロード」を待つ）
//   downloading  取得している（progress 0〜100）
//   downloaded   取得して SHA-512 も確かめた。「再起動して更新」を待つ（**黙って再起動しない**）
//   installing   再起動して更新している（保存の確認・エンジンの停止の後、quitAndInstall）
//   error        失敗した（error に利用者向けの短い文。次の確認でやり直す）
//
// 設定（userData の updates.json。原子的に書く）: channel（stable / beta）・autoCheck・autoDownload・lastVersion。
// beta と stable は GitHub Release の prerelease 印で分ける: beta の人だけ allowPrerelease。版が beta のアプリは既定で beta。
// **allowDowngrade は常に false**。electron-updater は channel を設定すると allowDowngrade を true にする
// （AppUpdater の set channel）ので、updater.channel は触らず、allowPrerelease を変えた後にも必ず上書きする。
import { EventEmitter } from 'node:events';
import fs from 'node:fs/promises';
import path from 'node:path';

export const PHASES = ['unavailable', 'idle', 'checking', 'current', 'available', 'downloading', 'downloaded', 'installing', 'error'];
export const CHANNELS = ['stable', 'beta'];

// 利用者に見せる失敗の文（生のエラーは見せない。ログには残す）
export const MESSAGES = {
  verify: '更新のファイルを確かめられませんでした。公式のページ（GitHub の Releases）から入れ直してください',
  network: '更新を確認できませんでした。インターネットの接続を確かめて、しばらくしてからもう一度お試しください',
  other: '更新を確認できませんでした。しばらくしてからもう一度お試しください',
};

const VERIFY_CODES = new Set(['ERR_UPDATER_INVALID_SIGNATURE', 'ERR_CHECKSUM_MISMATCH', 'ERR_UPDATER_NO_CHECKSUM',
  'ERR_UPDATER_INVALID_UPDATE_INFO', 'ERR_UPDATER_ZIP_FILE_NOT_FOUND']);
// 公開された版がまだ無い（安定版が 1 つも無いのに stable を選んだ、など）。失敗ではなく「新しい版は無い」
const NOTHING_CODES = new Set(['ERR_UPDATER_NO_PUBLISHED_VERSIONS', 'ERR_UPDATER_LATEST_VERSION_NOT_FOUND']);
const NETWORK_CODES = new Set(['ENOTFOUND', 'EAI_AGAIN', 'ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT', 'ENETUNREACH',
  'EHOSTUNREACH', 'ECONNABORTED', 'ERR_UPDATER_CHANNEL_FILE_NOT_FOUND']);

/** 失敗の種類: 'verify'（署名・チェックサム）/ 'nothing'（公開された版が無い）/ 'network' / 'other'。 */
export function errorKind(e) {
  const code = e?.code;
  if (VERIFY_CODES.has(code) || /sha512 checksum mismatch|not signed by the application owner/i.test(e?.message || '')) return 'verify';
  if (NOTHING_CODES.has(code)) return 'nothing';
  const status = e?.statusCode;
  if (NETWORK_CODES.has(code) || (status >= 400) || /net::ERR_|getaddrinfo|socket hang up|timed? ?out/i.test(e?.message || '')) return 'network';
  return 'other';
}

export function errorMessage(e) {
  return MESSAGES[errorKind(e)] || MESSAGES.other;
}

export class Updates extends EventEmitter {
  /**
   * @param {object} o
   * @param {object} o.updater  electron-updater の autoUpdater（か同じ形のフェイク）
   * @param {string} o.version  いまの版（app.getVersion()）
   * @param {string} o.file     設定の JSON（userData/updates.json）
   * @param {boolean} o.enabled 更新できるか（配布版で app-update.yml がある）
   * @param {boolean} [o.trackVersion]  「更新しました」のために版を覚えるか（配布版だけ。開発版が覚えると配布版で誤って出る）
   * @param {() => Promise<void>} [o.install]  再起動して更新（保存の確認・エンジンの停止・quitAndInstall は main がする）
   * @param {(e: Error) => void} [o.log]  生のエラーの記録
   * @param {() => number} [o.now]
   */
  constructor({ updater, version, file, enabled, trackVersion = enabled, install = async () => {}, log = () => {}, now = Date.now }) {
    super();
    Object.assign(this, { updater, version, file, enabled, trackVersion, install, log, now });
    this.busy = false;
    this.lastAttempt = 0;
    this.state = {
      version, enabled, phase: enabled ? 'idle' : 'unavailable',
      channel: version.includes('-') ? 'beta' : 'stable', autoCheck: true, autoDownload: true,
      target: null, progress: null, error: null, lastChecked: null, updatedFrom: null,
    };
    // 取得と適用は自分で呼ぶ。終了時の自動適用もしない（docs/release-plan.md §4）
    updater.autoDownload = false;
    updater.autoInstallOnAppQuit = false;
    this.applyChannel();
    updater.on('update-available', (info) => this.patch({ phase: 'available', target: info?.version || null, progress: null, error: null }));
    updater.on('update-not-available', () => this.patch({ phase: 'current', target: null, progress: null, error: null }));
    updater.on('download-progress', (p) => this.patch({ phase: 'downloading', progress: Math.max(0, Math.min(100, Math.floor(p?.percent || 0))) }));
    updater.on('update-downloaded', (info) => this.patch({ phase: 'downloaded', target: info?.version || this.state.target, progress: 100, error: null }));
    updater.on('error', (e) => this.fail(e));
  }

  snapshot() { return { ...this.state }; }

  patch(value) {
    Object.assign(this.state, value);
    this.emit('state', this.snapshot());
  }

  /** 失敗を状態に出す（electron-updater は error のイベントと Promise の reject の両方で知らせる。二度出しても同じ）。 */
  fail(e) {
    if (this.state.phase === 'installing') return;
    this.log(e);
    if (errorKind(e) === 'nothing') {
      this.patch({ phase: 'current', target: null, progress: null, error: null });
      return;
    }
    // 確認・取得のどちらの失敗も error にする。次の確認（自動・手動）でやり直す
    this.patch({ phase: 'error', progress: null, error: errorMessage(e) });
  }

  applyChannel() {
    this.updater.allowPrerelease = this.state.channel === 'beta';
    // allowPrerelease・channel の後に必ず上書きする（electron-updater は channel の設定でダウングレードを許す）
    this.updater.allowDowngrade = false;
  }

  /** 設定を読む。版が変わっていたら（更新した後の最初の起動）updatedFrom に前の版を入れる。 */
  async init() {
    let saved = {};
    try {
      saved = JSON.parse(await fs.readFile(this.file, 'utf8')) || {};
    } catch (e) {
      if (e.code !== 'ENOENT') this.log(e);   // 壊れていたら既定に戻す
    }
    if (CHANNELS.includes(saved.channel)) this.state.channel = saved.channel;
    this.state.autoCheck = saved.autoCheck !== false;
    this.state.autoDownload = saved.autoDownload !== false;
    if (this.trackVersion && typeof saved.lastVersion === 'string' && saved.lastVersion !== this.version) {
      this.state.updatedFrom = saved.lastVersion;
    }
    this.applyChannel();
    await this.save().catch((e) => this.log(e));
    return this.snapshot();
  }

  async save() {
    const prev = await fs.readFile(this.file, 'utf8').then(JSON.parse).catch(() => ({}));
    const data = {
      channel: this.state.channel, autoCheck: this.state.autoCheck, autoDownload: this.state.autoDownload,
      // 開発版は配布版の覚えた版を書き換えない
      lastVersion: this.trackVersion ? this.version : (prev?.lastVersion ?? null),
    };
    await fs.mkdir(path.dirname(this.file), { recursive: true });
    const tmp = `${this.file}.${process.pid}.tmp`;
    await fs.writeFile(tmp, JSON.stringify(data, null, 2) + '\n', 'utf8');
    await fs.rename(tmp, this.file);
  }

  /** 自動の確認（起動の少し後・一定間隔）。前回の確認（手動を含む）から minGap ミリ秒たっていなければ何もしない。投げない。 */
  async auto(minGap = 0) {
    if (!this.enabled || !this.state.autoCheck || this.busy) return false;
    // 見つけた版・取得した版を利用者の操作待ちで出している間は確かめ直さない（案内が消える）
    if (['available', 'downloading', 'downloaded', 'installing'].includes(this.state.phase)) return false;
    if (this.lastAttempt && this.now() - this.lastAttempt < minGap) return false;
    try { await this.check(); } catch { /* state.error に出ている */ }
    return true;
  }

  async exclusive(fn) {
    if (this.busy) throw new Error('更新の処理の途中');
    this.busy = true;
    try { return await fn(); } finally { this.busy = false; }
  }

  /** 確かめる（手動の「更新を確認」も）。見つかって自動ダウンロードがオンなら、そのまま取得する。 */
  async check() {
    if (!this.enabled) return this.snapshot();
    if (['downloading', 'downloaded', 'installing'].includes(this.state.phase)) return this.snapshot();
    return this.exclusive(async () => {
      this.lastAttempt = this.now();
      this.patch({ phase: 'checking', error: null, progress: null });
      try {
        this.applyChannel();
        await this.updater.checkForUpdates();
      } catch (e) {
        this.fail(e);
        return this.snapshot();
      }
      // checkForUpdates は available / not-available のイベントを出してから返る。どちらも来なかったら新しい版は無い
      if (this.state.phase === 'checking') this.patch({ phase: 'current' });
      this.patch({ lastChecked: new Date(this.now()).toISOString() });
      if (this.state.phase === 'available' && this.state.autoDownload) await this.fetch();
      return this.snapshot();
    });
  }

  /** 見つけた版を取得する（自動ダウンロードがオフのときの「ダウンロード」）。 */
  async download() {
    if (this.state.phase !== 'available') return this.snapshot();
    return this.exclusive(async () => { await this.fetch(); return this.snapshot(); });
  }

  async fetch() {
    this.patch({ phase: 'downloading', progress: 0, error: null });
    try {
      await this.updater.downloadUpdate();
    } catch (e) {
      this.fail(e);
      return;
    }
    // 取得と検証（SHA-512）が済むと update-downloaded が来る。来ていなければ失敗として扱う
    if (this.state.phase !== 'downloaded') this.fail(Object.assign(new Error('update-downloaded が来なかった'), { code: 'ERR_UPDATER_DOWNLOAD_INCOMPLETE' }));
  }

  /** 再起動して更新。取り消された（保存の確認でキャンセル）ら false を返し、取得した版はそのまま残す。 */
  async apply() {
    if (this.state.phase !== 'downloaded') throw new Error('更新はまだ準備できていない');
    return this.exclusive(async () => {
      this.patch({ phase: 'installing', error: null });
      try {
        const done = await this.install();
        if (done === false) this.patch({ phase: 'downloaded' });
        return done !== false;
      } catch (e) {
        this.log(e);
        this.patch({ phase: 'downloaded', error: `更新を始められませんでした: ${e.message}` });
        return false;
      }
    });
  }

  /** 設定を変える（自動確認・自動ダウンロード・beta を受け取る）。 */
  async setPreferences(value) {
    const next = {
      channel: CHANNELS.includes(value?.channel) ? value.channel : this.state.channel,
      autoCheck: typeof value?.autoCheck === 'boolean' ? value.autoCheck : this.state.autoCheck,
      autoDownload: typeof value?.autoDownload === 'boolean' ? value.autoDownload : this.state.autoDownload,
    };
    const prev = { channel: this.state.channel, autoCheck: this.state.autoCheck, autoDownload: this.state.autoDownload };
    Object.assign(this.state, next);
    try {
      await this.save();
    } catch (e) {
      Object.assign(this.state, prev);
      this.log(e);
      throw new Error('更新の設定を保存できなかった');
    }
    this.applyChannel();
    // チャネルを変えたら、前のチャネルで見つけた結果は捨てる（取得の途中・取得済みは残す）
    const keep = ['downloading', 'downloaded', 'installing'].includes(this.state.phase) || prev.channel === next.channel;
    this.patch(keep ? {} : { phase: this.enabled ? 'idle' : 'unavailable', target: null, progress: null, error: null });
    return this.snapshot();
  }

  /** 「<版> に更新しました」を閉じた（同じ起動の中で出し直さない。次の起動では lastVersion が同じなので出ない）。 */
  dismissNotice() {
    this.patch({ updatedFrom: null });
    return this.snapshot();
  }
}
