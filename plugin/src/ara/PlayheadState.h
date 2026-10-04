#pragma once

#include "RegionMapping.h"

#include <juce_audio_basics/juce_audio_basics.h>

#include <atomic>
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
};

/** processBlock（どのインスタンスでも）が書き、メッセージスレッドの 30 Hz のタイマーが読む。
    オーディオスレッドは原子変数に書くだけ（確保・ロックをしない）。値ごとの原子変数なので、読みと書きが重なると
    別のブロックの値が混ざりうるが、表示に使うだけなので許す。止まっているときに processBlock を呼ばないホストがあるので、
    最後の値を残す。 */
class PlayheadState
{
public:
    void write (const juce::AudioPlayHead::PositionInfo&) noexcept;
    PlayheadSnapshot read() const noexcept;

private:
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
