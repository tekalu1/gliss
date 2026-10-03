#pragma once

#include <juce_audio_basics/juce_audio_basics.h>
#include <juce_core/juce_core.h>

#include <memory>
#include <vector>

namespace gliss
{

/** 編集された 1 つの窓のデータ（ソースのサンプリング周波数・チャンネル数）。 */
struct EditedWindow
{
    juce::int64 startFrame = 0;       // ソースのサンプル位置
    juce::AudioBuffer<float> buffer;  // PCM データ (channels x frames)

    juce::int64 getEndFrame() const noexcept { return startFrame + buffer.getNumSamples(); }
    int getNumSamples() const noexcept { return buffer.getNumSamples(); }
    int getNumChannels() const noexcept { return buffer.getNumChannels(); }
};

/** ara_render_dirty で返される各窓のメタデータ。 */
struct WindowMeta
{
    juce::int64 startFrame = 0;
    int frames = 0;
    juce::int64 byteOffset = 0;
};

/** ある版（rev）における編集済み窓のスナップショット。
    不変（immutable）であり、オーディオスレッドは shared_ptr で安全に参照できる。 */
class EditedPcmSnapshot
{
public:
    EditedPcmSnapshot (juce::String rev,
                       double sampleRate,
                       int numChannels,
                       std::vector<EditedWindow> windows);
    ~EditedPcmSnapshot() = default;

    const juce::String& getRev() const noexcept { return rev; }
    double getSampleRate() const noexcept { return sampleRate; }
    int getNumChannels() const noexcept { return numChannels; }
    const std::vector<EditedWindow>& getWindows() const noexcept { return windows; }

    bool hasWindows() const noexcept { return ! windows.empty(); }

    /** 指定区間 [startFrame, startFrame + numFrames) に重なる窓を列挙する。 */
    void getOverlappingWindows (juce::int64 startFrame,
                                int numFrames,
                                std::vector<const EditedWindow*>& result) const;

private:
    juce::String rev;
    double sampleRate = 48000.0;
    int numChannels = 1;
    std::vector<EditedWindow> windows; // startFrame 昇順、重なりなし
};

/** AudioModification ごとの編集済み PCM キャッシュ。
    同期スレッドが applyDirty() で更新し、オーディオスレッドは tryGetSnapshot() で取得する。 */
class EditedPcm
{
public:
    EditedPcm (double sampleRate = 48000.0, int numChannels = 1);
    ~EditedPcm() = default;

    /** オーディオスレッド向け: スナップショットを非ブロッキングで取得する。
        ロックが取れなかった場合は nullptr を返す（原音フォールバック用）。 */
    std::shared_ptr<const EditedPcmSnapshot> tryGetSnapshot() const noexcept;

    /** 非オーディオスレッド向け: スナップショットを取得する。 */
    std::shared_ptr<const EditedPcmSnapshot> getSnapshot() const;

    /** 現在の版（rev）を取得する。 */
    juce::String getRev() const;

    /** 設定されているサンプリング周波数とチャンネル数を取得する。 */
    double getSampleRate() const noexcept { return sampleRate; }
    int getNumChannels() const noexcept { return numChannels; }

    /** フォーマットを設定する（変更時はスナップショットもクリア）。 */
    void setFormat (double newSampleRate, int newNumChannels);

    /** ara_render_dirty の結果を反映して新しいスナップショットを作成・差し替える。
        @param rev          新しい版
        @param reset        true なら既存の窓を全消去して空から開始
        @param restore      原音に戻す範囲 [startFrame, startFrame + frames)
        @param windows      新しい窓のメタデータ
        @param f32Stream    .f32 データのストリーム（float32 LE インターリーブ）
        @return 成功したかどうか
    */
    bool applyDirty (const juce::String& rev,
                     bool reset,
                     const std::vector<juce::Range<juce::int64>>& restore,
                     const std::vector<WindowMeta>& windows,
                     juce::InputStream& f32Stream);

    /** ファイルから applyDirty */
    bool applyDirty (const juce::String& rev,
                     bool reset,
                     const std::vector<juce::Range<juce::int64>>& restore,
                     const std::vector<WindowMeta>& windows,
                     const juce::File& f32File);

    /** メモリ上の raw float32 データから applyDirty（テスト用） */
    bool applyDirty (const juce::String& rev,
                     bool reset,
                     const std::vector<juce::Range<juce::int64>>& restore,
                     const std::vector<WindowMeta>& windows,
                     const float* f32Data,
                     size_t totalFloats);

    /** 全窓をクリアする */
    void clear();

private:
    static void applyRestoreRanges (std::vector<EditedWindow>& list,
                                    const std::vector<juce::Range<juce::int64>>& restore);

    static void removeOverlappingRanges (std::vector<EditedWindow>& list,
                                         juce::Range<juce::int64> rangeToRemove);

    double sampleRate = 48000.0;
    int numChannels = 1;

    mutable juce::SpinLock spinLock;
    std::shared_ptr<const EditedPcmSnapshot> currentSnapshot;
};

} // namespace gliss
