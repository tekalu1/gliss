// beta を受け取る人の「いちばん新しい版」を semver で選ぶ GitHub provider（公開リポジトリ用。認証なし）。
//
// electron-updater の GitHubProvider は、allowPrerelease のとき releases.atom（GitHub の Releases の並び）の
// **先頭から見て最初に条件に合う版**をそのまま最新とみなし、版の大小は比べない（GitHubProvider.getLatestVersion）。
// GitHub の並びは版の順ではない（作成日時でもない。2026-10-02 に tekalu1/pleiad の一覧で、後から作った
// android-v0.1.0-676 が beta.58 の後ろに並ぶのを確かめた）。Pleiad では v0.1.0-beta.9 が beta.11 より
// 前に並んで、beta.10 以降が見えなくなった（Pleiad の desktop/update-auth.cjs の NewestReleaseProvider）。
// そこで、feed の版をすべて semver で比べて一番新しいものを選び直す。stable の人（allowPrerelease なし）は
// 元の実装のまま（/releases/latest = GitHub が「Latest」とした正式版）。
//
// 各 Release の更新情報は latest.yml（beta の版でも。electron-builder の publish.channel: latest）。
// feed は GitHub が出す直近の 10 件だけなので、それより古い版は見ない（困らない）。
import { createRequire } from 'node:module';
import path from 'node:path';

const require = createRequire(import.meta.url);
const TAG_IN_LINK = /\/tag\/(v?[^/]+)$/;

/** feed の版（タグ）から一番新しいものを選ぶ。semver でないタグ（別の製品の Release など）は無視する。 */
export function newestTag(tags, semver) {
  const valid = tags.filter((t) => t && semver.valid(t));
  if (!valid.length) return null;
  return valid.sort((a, b) => semver.rcompare(a, b))[0];
}

function fromUpdater(name) {
  const dir = path.dirname(require.resolve('electron-updater/package.json'));
  return require(require.resolve(name, { paths: [dir] }));
}

/** resources/app-update.yml（electron-builder が publish から作る）を読む。 */
export function readUpdateConfig(text) {
  return fromUpdater('js-yaml').load(text) || {};
}

/** electron-updater の setFeedURL({ provider: 'custom', updateProvider }) に渡すクラスを作る。 */
export function newestReleaseProvider() {
  const { GitHubProvider } = require('electron-updater/out/providers/GitHubProvider');
  const { parseUpdateInfo } = require('electron-updater/out/providers/Provider');
  const { getChannelFilename, newUrlFromBase } = require('electron-updater/out/util');
  const rt = fromUpdater('builder-util-runtime');
  const semver = fromUpdater('semver');

  return class NewestReleaseProvider extends GitHubProvider {
    async getLatestVersion() {
      if (!this.updater.allowPrerelease) return super.getLatestVersion();
      const token = new rt.CancellationToken();
      const xml = await this.httpRequest(newUrlFromBase(`${this.basePath}.atom`, this.baseUrl),
        { accept: 'application/xml, application/atom+xml, text/xml, */*' }, token);
      let entries;
      try {
        entries = rt.parseXml(xml).getElements('entry').map((el) => {
          const m = TAG_IN_LINK.exec(el.element('link').attribute('href') || '');
          return { el, tag: m ? m[1] : null };
        });
      } catch (e) {
        throw rt.newError(`Cannot parse releases feed: ${e.message}`, 'ERR_UPDATER_INVALID_RELEASE_FEED');
      }
      const tag = newestTag(entries.map((e) => e.tag), semver);
      if (!tag) throw rt.newError('No published versions on GitHub', 'ERR_UPDATER_NO_PUBLISHED_VERSIONS');
      const entry = entries.find((e) => e.tag === tag).el;

      const channelFile = getChannelFilename(this.getDefaultChannelName());
      const url = newUrlFromBase(this.getBaseDownloadPath(tag, channelFile), this.baseUrl);
      let raw;
      try {
        raw = await this.executor.request(this.createRequestOptions(url), token);
      } catch (e) {
        if (e instanceof rt.HttpError && e.statusCode === 404) {
          throw rt.newError(`Cannot find ${channelFile} in the release ${tag} (${url})`, 'ERR_UPDATER_CHANNEL_FILE_NOT_FOUND');
        }
        throw e;
      }
      const result = parseUpdateInfo(raw, channelFile, url);
      if (result.releaseName == null) result.releaseName = entry.elementValueOrEmpty('title');
      if (result.releaseNotes == null) {
        const note = entry.elementValueOrEmpty('content');
        result.releaseNotes = note === 'No content.' ? '' : note;
      }
      return { tag, ...result };
    }
  };
}
