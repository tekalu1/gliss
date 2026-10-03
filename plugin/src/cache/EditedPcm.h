#pragma once

#include <juce_audio_basics/juce_audio_basics.h>
#include <juce_core/juce_core.h>

#include <memory>
#include <mutex>
#include <vector>

namespace gliss
{

/** 編集された 1 つの窓（ソースのサンプリング周波数・チャンネル数）。
    PCM は版をまたいで共有する。restore や重なりで切り詰めるときは offset と numSamples だけを変え、PCM を写さない。 */
struct EditedWindow
{
    juce::int64 startFrame = 0;                            // ソースのサンプル位置
    std::shared_ptr<const juce::AudioBuffer<float>> pcm;   // 窓の PCM（channels x frames）。切り詰めた窓は一部だけを使う
    int offset = 0;                                        // pcm の中の先頭
    int numSamples = 0;

    juce::int64 getEndFrame() const noexcept { return startFrame + numSamples; }
    int getNumSamples() const noexcept { return numSamples; }
    int getNumChannels() const noexcept { return pcm != nullptr ? pcm->getNumChannels() : 0; }

    /** 窓の先頭（startFrame）のサンプルを指す。 */
    const float* getReadPointer (int channel) const noexcept { return pcm->getReadPointer (channel) + offset; }
};

/** ara_render_dirty で返される各窓のメタデータ。 */
struct WindowMeta
{
    juce::int64 startFrame = 0;
    int frames = 0;
    juce::int64 byteOffset = 0;
};

/** ある版（rev）における編集済み窓のスナップショット。
    不変（immutable）であり、オーディオスレッドは shared_ptr で参照できる。 */
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

    /** frame より後ろで終わる最初の窓の添字（二分探索。無ければ getWindows().size()）。
        そこから startFrame が区間の終わりより前の窓をたどれば、区間に重なる窓を確保なしで列挙できる。 */
    size_t findFirstWindowEndingAfter (juce::int64 frame) const noexcept;

private:
    juce::String rev;
    double sampleRate = 48000.0;
    int numChannels = 1;
    std::vector<EditedWindow> windows; // startFrame 昇順、重なりなし
};

/** AudioModification ごとの編集済み PCM キャッシュ。
    同期スレッドが applyDirty() で更新し、オーディオスレッドは tryGetSnapshot() で取得する。

    スナップショットの解放: 差し替えた古いスナップショットは解放待ちの列に入れ、誰も参照しなくなったものを
    applyDirty()・setFormat()・clear()・releaseUnusedSnapshots()（いずれもオーディオスレッド以外）で解放する。
    オーディオスレッドが持つ参照は常に最後の参照ではないので、窓の PCM の解放がオーディオスレッドで起きない。
    更新が止まった後の解放待ちを落とすため、同期のスレッドなどから定期的に releaseUnusedSnapshots() を呼ぶ。 */
class EditedPcm
{
public:
    EditedPcm (double sampleRate = 48000.0, int numChannels = 1);
    ~EditedPcm() = default;

    /** オーディオスレッド向け: スナップショットを非ブロッキングで取得する。
        ロックが取れなかった場合は nullptr を返す（原音フォールバック用）。
        shared_ptr の写しと破棄は参照カウントの原子的な増減だけで、確保も解放も起きない。 */
    std::shared_ptr<const EditedPcmSnapshot> tryGetSnapshot() const noexcept;

    /** 非オーディオスレッド向け: スナップショットを取得する。 */
    std::shared_ptr<const EditedPcmSnapshot> getSnapshot() const;

    /** 現在の版（rev）を取得する。 */
    juce::String getRev() const;

    /** 設定されているサンプリング周波数とチャンネル数（書き手のスレッド用。オーディオスレッドはスナップショットの値を使う）。 */
    double getSampleRate() const noexcept { return sampleRate; }
    int getNumChannels() const noexcept { return numChannels; }

    /** フォーマットを設定する（スナップショットもクリア）。applyDirty() と同じスレッドから呼ぶ。 */
    void setFormat (double newSampleRate, int newNumChannels);

    /** ara_render_dirty の結果を反映して新しいスナップショットを作成・差し替える。
        失敗したとき（.f32 が読めない・短い）は差し替えない。
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

    /** 解放待ちのスナップショットのうち、もう誰も参照していないものを解放する（オーディオスレッド以外から呼ぶ）。
        @return まだ参照されていて残っている数 */
    size_t releaseUnusedSnapshots();

private:
    static void applyRestoreRanges (std::vector<EditedWindow>& list,
                                    const std::vector<juce::Range<juce::int64>>& restore);

    static void removeOverlappingRanges (std::vector<EditedWindow>& list,
                                         juce::Range<juce::int64> rangeToRemove);

    void publish (std::shared_ptr<const EditedPcmSnapshot> next);
    size_t releaseUnusedSnapshotsLocked();

    double sampleRate = 48000.0;
    int numChannels = 1;

    mutable juce::SpinLock spinLock;                          // currentSnapshot の差し替えと写しの間だけ
    std::shared_ptr<const EditedPcmSnapshot> currentSnapshot;

    std::mutex retiredLock;                                   // オーディオスレッドは触らない
    std::vector<std::shared_ptr<const EditedPcmSnapshot>> retired;
};

} // namespace gliss
