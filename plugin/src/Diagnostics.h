#pragma once

#include <juce_core/juce_core.h>

namespace gliss::diag
{

/** 環境変数 GLISS_ARA_TRACE_DIR が指すフォルダ。未設定なら無効（空の File）。

    開発・検証用。設定すると、プラグインの出来事と再生の集計を
    <フォルダ>/gliss-ara-<プロセス ID>.log に書く。オーディオスレッドからは書かない。
*/
const juce::File& traceDir();

bool traceEnabled();

/** 環境変数 GLISS_ARA_READ_TIMEOUT_MS。設定すると、リアルタイムの描画でも先読みの完了をこのミリ秒だけ待つ
    （検証用。ホストの音を CPU の速さで取りに来る検証ホストで、出力を欠けなく比べるために使う）。未設定なら -1。 */
int forcedReadTimeoutMs();

/** traceEnabled() のときだけ 1 行を追記する。どのスレッドからでもよいが、オーディオスレッドでは呼ばない。 */
void log (const juce::String& line);

/** 常に書く小さなログ（DAW の選択の判断など、実機で何が起きたかを後から調べるための行）。%APPDATA%\Gliss\plugin.log
    （GLISS_PLUGIN_STATE_FILE があればその隣。GLISS_PLUGIN_LOG_FILE で直接指定。1 MB を超えたら plugin.log.1 に回す）。
    同じ行が 2 秒以内に続いたら省き、1 秒に 20 行までにする。GLISS_ARA_TRACE_DIR が有効ならそちらにも書く。
    どのスレッドからでもよいが、オーディオスレッドでは呼ばない。 */
void logAlways (const juce::String& line);

} // namespace gliss::diag
