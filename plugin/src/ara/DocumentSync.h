#pragma once

#include "../cache/EditedPcm.h"
#include "../engine/McpClient.h"
#include "EngineCalls.h"
#include "NoteContent.h"
#include "RegionMapping.h"

#include <juce_core/juce_core.h>

#include <atomic>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <vector>

namespace gliss
{

/** ソースのサンプルを読む口（DAW の AudioSource をメッセージスレッドの外から読む。GlissDocumentController が ARA で実装する）。
    同期のスレッドから呼ぶ。読めない（ホストが音を変えた・読み出しを止めた・ソースが消えた）なら false。 */
struct SourceSamples
{
    virtual ~SourceSamples() = default;
    virtual bool read (float* const* dest, int numChannels, juce::int64 start, int numSamples) = 0;
};

/** 同期のスレッドに渡すドキュメントの形（メッセージスレッドが ARA のモデルから作る。ARA の型を持たない）。 */
struct SyncSource
{
    juce::String id;                           // AudioSource の persistentID
    double sampleRate = 0.0;
    int numChannels = 0;
    juce::int64 numSamples = 0;
    int generation = 0;                        // 音が変わる（読み直しが要る）たびに増える
    bool samplesAvailable = false;             // ホストが読み出しを許している
    std::shared_ptr<SourceSamples> samples;
};

struct SyncModification
{
    juce::String araId;                        // AudioModification の persistentID（エンジンの ara_id）
    juce::String sourceId;
    juce::String name, group;                  // group = 代表のリージョンの DAW のトラック名
    std::optional<double> offsetSec;           // 代表のリージョンでソースの 0 秒が置かれるソングの秒（リージョンが無ければ無し）
    juce::String cloneOf;                      // 複製元の persistentID（作られたときの）
    std::shared_ptr<EditedPcm> pcm;            // 再生のキャッシュ（オーディオスレッドが読む）
    std::vector<RegionTimes> regions;
};

struct SyncModel
{
    juce::String workKey, documentName;
    std::vector<SyncSource> sources;
    std::vector<SyncModification> modifications;
};

/** DAW のドキュメント 1 つとエンジン（McpClient 1 本）の間を同期する（design-stage23 §3-2・§3-4・§3-5・§4-2）。

    同期のスレッドが 1 本: エンジンを遅延起動して ara_open → ソースの音を一時 WAV に書いて ara_set_modification
    （アーカイブから戻すものは ara_restore）→ 位置・名前の変化は ara_sync → 外したものは ara_remove_modification →
    ara_revs で版の変わった修飾に ara_render_dirty（more の間は続ける）→ EditedPcm::applyDirty → ara_notes で DAW に返すノートの写し
    → ara_archive で保存用の写し。
    メッセージスレッドは setModel() で「あるべき形」を渡すだけで、エンジンを待たない（編集サイクルの後にまとめて渡す）。

    スレッド: setModel・setPending*・requestSync・requestEngine・restartEngine・get*・isSettled はどのスレッドからでもよい
    （isSettled はオーディオスレッドからも呼べる。原子変数を読むだけ）。callTool・waitForEngine・refreshArchives は待つので、
    オーディオスレッドからは呼ばない。Callbacks は同期のスレッドから呼ばれる。 */
class DocumentSync final : private juce::Thread
{
public:
    struct Options
    {
        bool engineDisabled = false;           // GLISS_ENGINE_DISABLED（検証用。エンジンを起動しない）
        std::optional<TestEdit> testEdit;      // GLISS_TEST_EDIT（試験用。最初の修飾に 1 回だけ当てる）
        double maxRenderSec = 10.0;            // ara_render_dirty の max_sec
        int pollMs = 1000;                     // ara_revs を見る間隔（裏の解析が済んだのを拾う）
    };

    struct Callbacks
    {
        std::function<void (const juce::String& name, const juce::var& data)> event;   // engine・cache・session-changed・project-changed・test-edit
        std::function<void (const juce::StringArray& araIds)> contentChanged;           // 再生の音が変わった修飾
        /** ノートが変わった修飾と、解析だけのノートが変わったソース（AudioSource の persistentID）。 */
        std::function<void (const juce::StringArray& araIds, const juce::StringArray& sourceIds)> notesChanged;
        std::function<void (const juce::String& line)> log;
    };

    struct EngineStatus
    {
        juce::String state { "idle" };         // idle・starting・ready・failed・disabled
        juce::String error;
    };

    struct ModStatus
    {
        juce::String trackId;
        juce::String state;                    // reading・waiting・syncing・ready・mismatch・failed（まだ何もしていなければ空）
        juce::String error, rev;
        double progress = 0.0;
    };

    DocumentSync (EngineConfig, Options, Callbacks);
    ~DocumentSync() override;

    /** 同期のスレッドとエンジンを止める（何度呼んでもよい）。以後は何もしない。 */
    void shutdown();

    void setModel (SyncModel);

    /** アーカイブから戻す編集（ソースがそろったら ara_restore）。素材が違えば戻さずに持ち続け、保存ではこれを書く。 */
    void setPendingRestore (const juce::String& araId, const juce::var& archive);

    /** アーカイブのガイド（修飾の persistentID）。登録が済んだら ara_sync(guide=…) で当てる。 */
    void setPendingGuide (const juce::String& araId);

    void requestSync();
    void requestEngine();
    void restartEngine();

    /** 画面の engineCall。エンジンの準備（ara_open）を最大 60 秒待ってから呼ぶ。失敗も {ok:false} の値で返す。 */
    juce::var callTool (const juce::String& tool, const juce::var& args, int timeoutMs);

    /** エンジンが ready・failed・disabled になるまで待つ。ready なら true。 */
    bool waitForEngine (int timeoutMs);

    /** 同期すべきものが残っていないか（オーディオスレッドからも呼べる）。非リアルタイムの描画が待つのに使う。 */
    bool isSettled() const noexcept { return settled.load (std::memory_order_acquire); }

    /** ara_open を済ませた（作業場所の鍵が決まった。以後アーカイブの鍵で変えない）。 */
    bool hasOpened() const noexcept { return openedOnce.load(); }

    EngineStatus getEngineStatus() const;
    ModStatus getModStatus (const juce::String& araId) const;
    juce::String findAraIdForTrack (const juce::String& trackId) const;
    juce::File getWorkDir() const;
    juce::String getOpenedWorkKey() const;

    /** 保存の前に呼ぶ（待つ）。エンジンが動いていれば ara_archive で保存用の写しを取り直す。 */
    void refreshArchives (int timeoutMs);

    /** 保存に書く編集: 戻している途中・素材が違って当てていないアーカイブがあればそれ、無ければエンジンの最新の写し（無ければ void）。 */
    juce::var getArchiveForStore (const juce::String& araId) const;

    /** 保存に書くガイドの修飾（無ければ空）。 */
    juce::String getGuideForStore() const;

    /** DAW に返すノートの写し（まだ無ければ nullptr）。エンジンを待たない（ARA の content reader が呼ぶ）。 */
    std::shared_ptr<const ModificationNotes> getNotes (const juce::String& araId) const;

private:
    struct PendingRestore
    {
        juce::var archive;
        int serial = 0;
        int attemptedGeneration = -1;          // そのソースの世代で一度当ててみた
        bool mismatch = false;
        juce::String reason, editSignature;    // 素材違いのときの理由・その時の編集の署名（変われば利用者が編集した＝捨てる）
    };

    struct Captured
    {
        int generation = -1;
        bool ok = false;
        juce::File wav;
    };

    struct Applied
    {
        bool registered = false, everRegistered = false, failed = false;
        int sourceGeneration = -1;
        juce::String trackId, name, group;
        std::optional<double> offsetSec;
    };

    void run() override;
    void cycle();
    bool openEngine (const SyncModel&);
    void resetEngineSession();
    bool captureSource (const SyncSource&, const SyncModel&);
    bool registerModification (const SyncModification&, const SyncSource&, juce::StringArray& changed);
    void restoreIfPending (const juce::String& araId, int generation, juce::StringArray& changed);
    bool renderModification (const SyncModification&, juce::StringArray& contentChanged);
    void refreshNotes (const SyncModel&, const std::map<juce::String, juce::String>& targets);
    void applyTestEdit (const SyncModel&);
    void refreshArchivesLocked (const juce::var& args, int timeoutMs);
    juce::var call (const juce::String& tool, const juce::var& args, int timeoutMs);

    void setEngineState (const juce::String& state, const juce::String& error = {});
    void setModState (const juce::String& araId, const juce::String& state, double progress = 0.0, const juce::String& error = {});
    void setModTrack (const juce::String& araId, const juce::String& trackId, const juce::String& rev);
    void emit (const juce::String& name, const juce::var& data);
    void log (const juce::String& line);

    Options options;
    Callbacks callbacks;
    juce::String engineSource, engineExecutable;   // ログ用（EngineConfig::source と実行ファイルの名前）

    // ---- 共有（mutex で守る） ----
    mutable std::mutex mutex;
    SyncModel model;
    std::map<juce::String, PendingRestore> pendingRestores;
    juce::String pendingGuide;
    bool guidePending = false;
    std::map<juce::String, juce::var> latestArchives;
    juce::String latestGuide;
    EngineStatus engineStatus;
    std::map<juce::String, ModStatus> modStatus;
    std::map<juce::String, std::shared_ptr<const ModificationNotes>> notesByMod;
    juce::File workDir;
    juce::String openedKey;
    int restoreSerial = 0;

    std::atomic<bool> settled { true }, dirty { false }, engineWanted { false }, restartRequested { false },
                      engineDied { false }, shuttingDown { false }, openedOnce { false };

    // ---- 同期のスレッドだけ ----
    bool opened = false, engineFailed = false, testEditDone = false, sessionChanged = false;
    std::map<juce::String, Captured> captured;              // ソースの persistentID
    std::map<juce::String, Applied> applied;                // ara_id
    std::map<juce::String, juce::String> localRev;          // ara_id → 手元のキャッシュの版
    std::map<juce::String, juce::String> notesRev;          // ara_id → ノートの写しを取ったときの ara_revs の版

    McpClient mcp;   // 最後に置く（先に壊れて、終了の通知が上の原子変数より後に来ないように）
};

} // namespace gliss
