#pragma once

#include "EditedPcm.h"
#include "SourceReader.h"

#include <juce_audio_basics/juce_audio_basics.h>
#include <juce_core/juce_core.h>

#include <vector>

namespace gliss
{

/** リサンプリング等の再生状態を保持するクラス（PlaybackRenderer または Region ごとに保持）。 */
class ResampleState
{
public:
    ResampleState();
    ~ResampleState() = default;

    /** 状態をリセットする（シーク時や周波数変更時） */
    void reset() noexcept;

    /** 連続性を判定し、シークと判定された場合はリセットして新しい開始位置を設定する。
        @param startInSource  今回のブロックで期待されるソース開始サンプル
        @return 連続していると判定されたソース開始サンプル
    */
    juce::int64 checkContinuityAndGetStart (juce::int64 startInSource) noexcept;

    /** 今回のブロックで消費された入力サンプル数だけ位置を進める */
    void advanceSourceSamples (int numUsedSourceSamples) noexcept;

    /** 指定チャンネルの補間器を取得する（必要に応じてリサイズ） */
    juce::WindowedSincInterpolator& getInterpolator (int channel) noexcept;

private:
    juce::int64 nextExpectedSourceSample = -1;
    std::vector<juce::WindowedSincInterpolator> interpolators;
};

/** 読み出しオプション */
struct RegionReadOptions
{
    bool compareMode = false;      // true なら原音のみ（編集窓を適用しない）
    bool addToDestBuffer = false;  // true なら出力バッファに加算、false なら上書き
    int timeoutMs = 0;             // 原音読み出しタイムアウト（リアルタイム時は 0）
};

/** 1 ブロックを「窓＋原音＋周波数変換（同じなら素通し）＋チャンネル数の変換」で読む。 */
class RegionReader
{
public:
    /** 1 ブロックのレンダリングを行う。
        オーディオスレッドから安全に呼べる（動的メモリ確保なし、tryLock で待たない）。

        @param destBuffer       出力先バッファ（ホストのサンプリング周波数、ホストのチャンネル数）
        @param destStartSample  出力先バッファ内の書き込み開始インデックス
        @param numDestSamples   書き込むサンプル数
        @param startInSource    ソースの時間軸（ソースのサンプリング周波数）での読み出し開始位置
        @param hostSampleRate   ホストのサンプリング周波数
        @param sourceReader     原音リーダー（nullptr の場合は無音）
        @param editedPcm        編集済み PCM キャッシュ（nullptr の場合は原音のみ）
        @param resampleState    リサンプリング状態（周波数が異なる場合に使用。nullptr の場合は毎回リセット）
        @param options          読み出しオプション（比較モード、加算モード、タイムアウト等）
        @return 完全に読めたかどうか（原音が不足なく読めれば true）
    */
    static bool readBlock (juce::AudioBuffer<float>& destBuffer,
                           int destStartSample,
                           int numDestSamples,
                           juce::int64 startInSource,
                           double hostSampleRate,
                           SourceReader* sourceReader,
                           const EditedPcm* editedPcm,
                           ResampleState* resampleState = nullptr,
                           const RegionReadOptions& options = {}) noexcept;
};

} // namespace gliss
