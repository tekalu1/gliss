#pragma once

#include "RegionMapping.h"

#include <juce_audio_basics/juce_audio_basics.h>

#include <atomic>
#include <cstdint>
#include <optional>
#include <utility>
#include <vector>

namespace gliss
{

/** DAW の再生位置の写し（docs/ara-plugin.md の「再生」）。 */
struct PlayheadSnapshot
{
    bool valid = false;          // 一度でも書かれた
    double songSec = 0.0;
    bool playing = false;
    bool looping = false;
    double loopStartSec = 0.0, loopEndSec = 0.0;
    juce::uint32 stampMs = 0;    // 書いた時刻（Time::getMillisecondCounter）
    std::uint64_t sequence = 0; // 受信側で古い通知を捨てるための版
};

/** オーディオスレッドは待たずに一つのインスタンスの位置を公開する。
    読み手は sequence の前後一致で一つのブロックの値だけを受け取る。 */
class PlayheadState
{
public:
    void write (const juce::AudioPlayHead::PositionInfo&, std::uintptr_t source = 1) noexcept;
    PlayheadSnapshot read() const noexcept;

private:
    std::atomic<bool> writing { false };
    std::atomic<std::uint64_t> sequence { 0 };
    std::uintptr_t masterSource = 0; // writing を獲得したスレッドだけが触る
    std::atomic<bool> valid { false }, playing { false }, looping { false };
    std::atomic<double> songSec { 0.0 }, loopStartSec { 0.0 }, loopEndSec { 0.0 };
    std::atomic<juce::uint32> stampMs { 0 };
};

namespace playhead
{
/** PPQ の位置を秒に直す（今の位置 ppqPosition・timeSec と bpm から。テンポが一定の区間で正しい）。 */
std::optional<double> ppqToSeconds (double ppq, double ppqPosition, double timeSec, double bpm);

/** 画面への playhead のイベント { song_sec, playing, loop: [a, b] | null, mapped: { "<track_id>": <編集の秒> | null } }。
    mapped は各トラックの「いま鳴っているリージョンの中の編集の秒」（鳴っていなければ null）。 */
juce::var describe (const PlayheadSnapshot&, const std::vector<std::pair<juce::String, std::vector<RegionTimes>>>& tracks);
} // namespace playhead

} // namespace gliss
