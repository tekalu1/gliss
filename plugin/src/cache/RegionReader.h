#pragma once

#include "EditedPcm.h"
#include "SourceReader.h"

#include <juce_audio_basics/juce_audio_basics.h>
#include <juce_core/juce_core.h>

#include <memory>

namespace gliss
{

/** 読み出しオプション */
struct RegionReadOptions
{
    bool compareMode = false;      // true なら原音のみ（編集窓を適用しない）
    bool addToDestBuffer = false;  // true なら出力バッファに加算、false なら上書き
    int timeoutMs = 0;             // 原音読み出しタイムアウト（リアルタイム時は 0）
};

/** 1 つのリージョンの再生の読み出し（PlaybackRenderer × PlaybackRegion ごとに 1 つ持つ）。
    1 ブロックを「窓＋原音＋周波数変換（同じなら素通し）＋チャンネル数の変換」で読む。

    周波数が違うときは、原音と窓を合わせた「ソースの周波数の列」を流しで変換する（WindowedSincInterpolator）。
    ブロックの頭が前のブロックの続きでなければ（シーク・ループ）状態を捨て、新しい位置の前後のソースで変換器を満たしてから読む。
    変換器の遅れ（ソースの 100 サンプル）はこのとき先読みで打ち消すので、出力の時刻は素通しと同じになる。

    スレッド: prepare()・releaseResources() は readBlock() と同時に呼ばない（prepareToPlay・releaseResources で呼ぶ）。
    readBlock()・reset() はオーディオスレッドから呼べる（確保・ロック待ち・例外・ファイルの入出力・juce::String の生成をしない。
    原音の先読みの待ちは options.timeoutMs まで）。 */
class RegionReader
{
public:
    /** 扱うソースのチャンネル数の上限（AudioBuffer が参照の配列を確保せずに持てる数） */
    static constexpr int maxChannels = 31;

    RegionReader();
    ~RegionReader();

    /** 作業用のバッファと変換器を確保する。
        @param hostSampleRate     再生の周波数
        @param maxBlockSize       readBlock() に渡すブロック長の上限（超えたら中で分けて読む）
        @param maxSourceChannels  ソースのチャンネル数の上限（超えた分のチャンネルは読まない。maxChannels まで）
        @param sourceSampleRate   ソースの周波数（作業用のバッファの長さを決める。違う周波数のソースでも中で分けて読む）
    */
    void prepare (double hostSampleRate, int maxBlockSize, int maxSourceChannels, double sourceSampleRate);

    /** 作業用のバッファを解放する。 */
    void releaseResources();

    bool isPrepared() const noexcept { return sourceCapacity > 0; }

    /** 周波数変換の状態を捨てる（次のブロックで新しい位置から満たし直す）。 */
    void reset() noexcept;

    /** 1 ブロックのレンダリングを行う。prepare() の後、オーディオスレッドから呼ぶ。
        prepare() していなければ無音（加算なら何もしない）で false を返す。

        @param destBuffer       出力先バッファ（ホストのサンプリング周波数、ホストのチャンネル数）
        @param destStartSample  出力先バッファ内の書き込み開始インデックス
        @param numDestSamples   書き込むサンプル数
        @param startInSource    ソースの時間軸（ソースのサンプリング周波数）での読み出し開始位置
        @param sourceReader     原音リーダー（nullptr の場合は無音）
        @param editedPcm        編集済み PCM キャッシュ（nullptr の場合は原音のみ）
        @param options          読み出しオプション（比較モード、加算モード、タイムアウト等）
        @return 原音の先読みが間に合わずに無音で埋めた区間が無ければ true
    */
    bool readBlock (juce::AudioBuffer<float>& destBuffer,
                    int destStartSample,
                    int numDestSamples,
                    juce::int64 startInSource,
                    SourceReader* sourceReader,
                    const EditedPcm* editedPcm,
                    const RegionReadOptions& options = {}) noexcept;

private:
    struct Source;

    bool prime (juce::int64 startInSource, int numChannels, const Source& source) noexcept;

    double hostSampleRate = 0.0;
    int blockCapacity = 0;      // 1 回に変換して書く長さの上限
    int channelCapacity = 0;
    int sourceCapacity = 0;     // ソースの周波数の列を 1 回に読む長さの上限

    juce::AudioBuffer<float> sourceWork;   // ソースの周波数の列（原音＋窓）
    juce::AudioBuffer<float> outputWork;   // 変換した列
    std::unique_ptr<juce::WindowedSincInterpolator[]> interpolators;

    // 周波数変換の流しの状態
    bool primed = false;
    double primedRatio = 0.0;
    int primedChannels = 0;
    juce::int64 nextReadPosition = 0;      // 次に変換器へ入れるソースの位置
    double expectedStartInSource = 0.0;    // 続きのブロックなら来るはずの startInSource
};

} // namespace gliss
