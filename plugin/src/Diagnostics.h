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

} // namespace gliss::diag
