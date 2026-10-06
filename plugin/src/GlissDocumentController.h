#pragma once

#include <juce_audio_formats/juce_audio_formats.h>
#include <juce_audio_processors/juce_audio_processors.h>

#include "ara/DocumentBridge.h"
#include "ara/DocumentSync.h"
#include "ara/PlayheadState.h"
#include "ara/PreviewAudio.h"
#include "cache/EditedPcm.h"

#include <map>
#include <memory>
#include <mutex>
#include <set>

namespace gliss
{

/** PlaybackRenderer が DocumentController に頼むもの（オーディオスレッドから呼ぶ。待たない）。 */
struct RenderContext
{
    virtual ~RenderContext() = default;

    /** ドキュメントの編集中かどうかを待たずに確かめる。取れなければ編集中。 */
    virtual juce::ScopedTryReadLock getProcessingLock() = 0;

    /** 原音と比べる（編集を外して鳴らす）。 */
    virtual bool isCompareMode() const noexcept = 0;

    /** DAW との同期（ソースの読み込み・エンジン・差分の再合成）が済んでいるか。非リアルタイムの描画が待つのに使う。 */
    virtual bool isSyncSettled() const noexcept = 0;
};

/** 編集の単位（docs/ara-plugin.md の「編集の単位・保存」）。エンジンのボーカルのトラック 1 本（ara_id = persistentID）。
    編集を当てた音のキャッシュ（EditedPcm）を持つ。同じ修飾の複数のリージョンは同じキャッシュを読む。 */
class GlissAudioModification final : public juce::ARAAudioModification
{
public:
    GlissAudioModification (juce::ARAAudioSource* audioSource,
                            ARA::ARAAudioModificationHostRef hostRef,
                            const juce::ARAAudioModification* optionalModificationToClone);

    /** 再生のキャッシュ。同期のスレッドとこの修飾が持ち合う（どちらが先に消えてもよい）。 */
    const std::shared_ptr<EditedPcm>& getEditedPcm() const noexcept { return editedPcm; }

    /** 複製元の persistentID（DAW の「固有にする」など。複製でなければ空）。 */
    const juce::String& getCloneSourceId() const noexcept { return cloneSourceId; }

private:
    std::shared_ptr<EditedPcm> editedPcm;
    juce::String cloneSourceId;
};

/** DocumentController の実装（docs/ara-plugin.md の「エンジンとの同期」「編集の単位・保存」「再生」）。エンジン（DocumentSync の McpClient）を 1 本持ち、
    ARA の出来事をエンジンに伝え、編集を当てた音を再生のキャッシュに置き、DAW のソングに編集リストを保存する。
    エディタ（C4）には DocumentBridge として見せる。 */
class GlissDocumentController final : public juce::ARADocumentControllerSpecialisation,
                                      public DocumentBridge,
                                      private RenderContext,
                                      private juce::Timer
{
public:
    GlissDocumentController (const ARA::PlugIn::PlugInEntry* entry, const ARA::ARADocumentControllerHostInstance* instance);
    ~GlissDocumentController() override;

    /** processBlock（どのインスタンスでも）が DAW の再生位置を書く。 */
    PlayheadState& getPlayheadState() noexcept { return playheadState; }

    // ---- DocumentBridge ----
    void engineCall (const juce::String& tool, const juce::var& args, Completion done) override;
    juce::var bootstrap() override;
    void saveState (const juce::var& patch) override;
    juce::var transport (const juce::String& op, const juce::var& arg) override;
    void preview (const juce::String& op, const juce::var& arg, Completion done) override;
    void setCompare (bool on) override;
    juce::var hostState() override;
    void restartEngine (Completion done) override;
    bool isReadableByEditor (const juce::File& file) override;
    void addListener (Listener*) override;
    void removeListener (Listener*) override;
    void editorSelectionChanged (const juce::ARAViewSelection& selection) override;

protected:
    void willBeginEditing (juce::ARADocument*) override;
    void didEndEditing (juce::ARADocument*) override;
    void willDestroyDocument (juce::ARADocument*) override;
    void didEnableAudioSourceSamplesAccess (juce::ARAAudioSource*, bool enable) override;
    void doUpdateAudioSourceContent (juce::ARAAudioSource*, juce::ARAContentUpdateScopes) override;
    void willDestroyAudioSource (juce::ARAAudioSource*) override;

    juce::ARAAudioModification* doCreateAudioModification (juce::ARAAudioSource* audioSource,
                                                           ARA::ARAAudioModificationHostRef hostRef,
                                                           const juce::ARAAudioModification* optionalModificationToClone) noexcept override;
    juce::ARAPlaybackRenderer* doCreatePlaybackRenderer() noexcept override;
    juce::ARAEditorRenderer* doCreateEditorRenderer() noexcept override;

    bool doRestoreObjectsFromStream (juce::ARAInputStream& input, const juce::ARARestoreObjectsFilter* filter) noexcept override;
    bool doStoreObjectsToStream (juce::ARAOutputStream& output, const juce::ARAStoreObjectsFilter* filter) noexcept override;

    // DAW に返すノート（kARAContentTypeNotes）。中身は同期のスレッドが取ったエンジンのノートの写し（エンジンを待たない）。
    // AudioSource = 解析だけ（detected）、AudioModification = 編集を当てた後（ソースの秒）、PlaybackRegion = それをリージョンで切り
    // ソングの秒に写したもの。編集があれば adjusted。
    bool doIsAudioSourceContentAvailable (const ARA::PlugIn::AudioSource*, ARA::ARAContentType) override;
    ARA::ARAContentGrade doGetAudioSourceContentGrade (const ARA::PlugIn::AudioSource*, ARA::ARAContentType) override;
    ARA::PlugIn::ContentReader* doCreateAudioSourceContentReader (ARA::PlugIn::AudioSource*, ARA::ARAContentType,
                                                                 const ARA::ARAContentTimeRange*) override;
    bool doIsAudioModificationContentAvailable (const ARA::PlugIn::AudioModification*, ARA::ARAContentType) override;
    ARA::ARAContentGrade doGetAudioModificationContentGrade (const ARA::PlugIn::AudioModification*, ARA::ARAContentType) override;
    ARA::PlugIn::ContentReader* doCreateAudioModificationContentReader (ARA::PlugIn::AudioModification*, ARA::ARAContentType,
                                                                       const ARA::ARAContentTimeRange*) override;
    bool doIsPlaybackRegionContentAvailable (const ARA::PlugIn::PlaybackRegion*, ARA::ARAContentType) override;
    ARA::ARAContentGrade doGetPlaybackRegionContentGrade (const ARA::PlugIn::PlaybackRegion*, ARA::ARAContentType) override;
    ARA::PlugIn::ContentReader* doCreatePlaybackRegionContentReader (ARA::PlugIn::PlaybackRegion*, ARA::ARAContentType,
                                                                    const ARA::ARAContentTimeRange*) override;
    bool doIsAudioSourceContentAnalysisIncomplete (const ARA::PlugIn::AudioSource*, ARA::ARAContentType) override;
    void doRequestAudioSourceContentAnalysis (ARA::PlugIn::AudioSource*, std::vector<ARA::ARAContentType> const&) override;

private:
    class AraSourceSamples;
    class TestBridge;

    /** AudioSource ごとの読み出し（メッセージスレッドだけが触る）。 */
    struct SourceEntry
    {
        std::unique_ptr<juce::ARAAudioSourceReader> reader;   // ホストのリーダーはメッセージスレッドで作り・壊す（ARA の決まり）
        std::shared_ptr<AraSourceSamples> samples;            // 同期のスレッドが読む口
        int generation = 0;
        bool contentChanged = false;
    };

    /** 画面に見せる修飾（メッセージスレッドの写し）。 */
    struct TrackView
    {
        juce::String araId;
        std::vector<RegionTimes> regions;
    };

    // RenderContext
    juce::ScopedTryReadLock getProcessingLock() override;
    bool isCompareMode() const noexcept override { return compare.load(); }
    bool isSyncSettled() const noexcept override;

    void timerCallback() override;

    void pushModel();
    void ensureReader (juce::ARAAudioSource*, SourceEntry&);
    void dropSourceEntry (juce::ARAAudioSource*);
    /** notifyHost が false なら、ARA のリスナーにだけ知らせる（ホストには知らせない）。 */
    void notifyContentChanged (const juce::StringArray& araIds, bool notifyHost = true);
    void notifyNotesChanged (const juce::StringArray& araIds, const juce::StringArray& sourceIds, bool firstContent);
    /** 保存するもの（アーカイブ）だけが変わった: 修飾には音・ノートの変わらない知らせを、documentData なら文書の知らせも送る。 */
    void notifyStateChanged (const juce::StringArray& araIds, bool documentData);
    /** ホストに渡すノート（読めなければ nullptr。ホストに「まだ無い」と答えたことを覚える）。 */
    std::shared_ptr<const ModificationNotes> notesOf (const ARA::PlugIn::AudioModification*) const;
    std::shared_ptr<const ModificationNotes> sourceNotesOf (const ARA::PlugIn::AudioSource*) const;
    std::shared_ptr<const ModificationNotes> readyNotes (const ARA::PlugIn::AudioModification*) const;
    bool takeHostSawNoNotes (const juce::String& key);
    void postEvent (const juce::String& name, const juce::var& data);
    void sendEvent (const juce::String& name, const juce::var& data);
    void onSyncEvent (const juce::String& name, const juce::var& data);
    juce::var describeSelection() const;
    juce::var describePlayhead() const;
    juce::var describePlayhead (const PlayheadSnapshot&) const;
    juce::var loadedState();
    const TrackView* findTrackByTrackId (const juce::String& trackId) const;
    std::optional<double> toSongSeconds (const juce::var& arg, const juce::String& secKey) const;

    juce::ReadWriteLock processBlockLock;
    bool editing = false;
    bool hostLogged = false;

    juce::String workKey;
    std::unique_ptr<DocumentSync> sync;
    PlayheadState playheadState;
    std::shared_ptr<PreviewAudio> previewAudio = std::make_shared<PreviewAudio>();
    std::atomic<std::uint64_t> previewGeneration { 0 };
    std::atomic<bool> compare { false };

    // ホストにノートを「まだ無い」と答えた修飾（m:<ID>）・ソース（s:<ID>）。ホストの読み出しのスレッドとメッセージスレッドが触る
    mutable std::mutex hostNotesMutex;
    mutable std::set<juce::String> hostSawNoNotes;

    std::map<juce::ARAAudioSource*, SourceEntry> sourceEntries;
    std::vector<std::unique_ptr<juce::ARAAudioSourceReader>> retiredReaders;
    std::vector<TrackView> tracks;

    juce::ListenerList<Listener> listeners;
    juce::String selectionAraId;
    RegionTimes selectionRegion;
    bool hasSelection = false;
    juce::String lastPlayheadJson;
    bool lastHostPlaying = false;

    juce::var pluginState;
    bool pluginStateLoaded = false;

    std::shared_ptr<bool> alive = std::make_shared<bool> (true);
    std::unique_ptr<TestBridge> testBridge;   // GLISS_TEST_BRIDGE_DIR（試験用の画面の代わり。GLISS_TEST_HOOKS のビルドだけ）
    juce::ThreadPool bridgePool { 2 };   // engineCall・restartEngine・saveState の書き込み（メッセージスレッドで待たない）
};

} // namespace gliss
