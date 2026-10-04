#pragma once

#include "../cache/EditedPcm.h"

#include <juce_core/juce_core.h>

#include <optional>
#include <vector>

namespace gliss
{

namespace tools
{
/** 画面（engineCall）から呼ばせないツール。プラグインではドキュメント・トラックは DAW が決め、書き出しは DAW のバウンスで行う。
    ara_* はプラグイン（C++）だけが呼ぶ。 */
bool isForbidden (const juce::String& tool);

/** 画面の呼び出しが成功した後に DAW との同期（ara_revs → ara_render_dirty）を予約するか。
    「読むだけ」の一覧に頼らず、読むだけのもの以外は全部予約する（書き込むツールを列挙して漏らさない）。
    get_job はジョブが終わったときだけ（解析が終わると窓の音が変わる）。 */
bool shouldSyncAfter (const juce::String& tool, const juce::var& result);
} // namespace tools

/** 試験用の口 GLISS_TEST_EDIT の中身（エンジンにつないで解析が済んだら、最初の修飾に 1 回だけ当てる編集）。
    受ける形: {"tool": "shift_pitch", "args": {...}} ／ shift_pitch の引数そのもの {"cents": 100, "start_sec": 0, "end_sec": 5} ／
    "shift_pitch:<note_id>:<cents>"（docs/ara-plugin.md の「検証用の環境変数」）。 */
struct TestEdit
{
    juce::String tool;
    juce::var args;

    static std::optional<TestEdit> parse (const juce::String& text);
};

/** ara_render_dirty の結果（engine/docs/MCP.md §3-4）。 */
struct DirtyUpdate
{
    juce::String rev;
    bool reset = false, more = false, analysisPending = false;
    double sampleRate = 0.0;
    int numChannels = 0;
    juce::int64 sourceFrames = 0;
    std::vector<juce::Range<juce::int64>> restore;
    std::vector<WindowMeta> windows;
    juce::File path;   // windows が無ければ空

    /** 読めなければ false と理由（ok:false・形の違い）。 */
    static bool parse (const juce::var& result, DirtyUpdate& out, juce::String& error);
};

/** ara_revs の external（外部の AI の中継の番号。engine/vocal_engine/ara_relay.py）を見て、外部の AI が曲を変えたかを判断する。
    同期のスレッドだけが使う。最初に見た番号は覚えるだけ（知らせない）。中継が無い（null）・エンジンを起動し直したら忘れる。 */
struct ExternalChanges
{
    struct Result
    {
        bool projectChanged = false, sessionChanged = false;
        juce::String trackId;   // 最後に外部が変えたトラック
    };

    Result update (const juce::var& revsResult);
    void reset() noexcept { seq = sessionSeq = -1; }

private:
    juce::int64 seq = -1, sessionSeq = -1;
};

/** エンジンの結果が失敗か（{ok:false}）。 */
bool isFailure (const juce::var& result);

/** 失敗の理由（error、無ければ既定の文）。 */
juce::String failureReason (const juce::var& result);

} // namespace gliss
