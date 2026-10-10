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
#include <set>
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

/** DAW のドキュメント 1 つとエンジン（McpClient 1 本）の間を同期する（docs/ara-plugin.md の「エンジンとの同期」、engine/docs/MCP.md §3-4）。

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
        /** 再生の音が変わった修飾。notifyHost が false なら、登録・アーカイブから戻した状態に追いついただけ
            （最初の再合成・解析待ちの後の再合成。ARA は「戻した状態と違うときだけ知らせる」。ホストには知らせない）。 */
        std::function<void (const juce::StringArray& araIds, bool notifyHost)> contentChanged;
        /** ノートが変わった修飾と、解析だけのノートが変わったソース（AudioSource の persistentID）。
            firstContent: 登録してから初めてノートが読めるようになった（それまで読めなかった）もの。 */
        std::function<void (const juce::StringArray& araIds, const juce::StringArray& sourceIds, bool firstContent)> notesChanged;
        /** 保存するもの（アーカイブ）が、ホストに知らせた後に変わった（音・ノートの知らせは出していない）。araIds: 修飾の
            保存の状態（編集の履歴・歌詞・F0 の方式）が変わった修飾と、ガイドの指定が変わった修飾。documentData: 文書の
            保存の状態（ガイドの指定）が変わった。ホストが保存を求めないと失われる（ARA は確実に知らせることを求める）。 */
        std::function<void (const juce::StringArray& araIds, bool documentData)> stateChanged;
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

    /** アーカイブのトラックごとのガイド（修飾 → ガイドの修飾。値が空ならその修飾の指定を外す = アーカイブを正にする）。
        両方の登録が済んだものから ara_sync(guides=…) で当てる。エンジンが答えたもの（当てた・断られた）は残さない。 */
    void setPendingGuides (const std::map<juce::String, juce::String>& guides);

    /** ホストが持っている文書のガイドの指定（アーカイブから戻したもの。修飾 → ガイドの修飾・共通のガイド）。保存に書く
        指定がこれと違ってきたら、ホストに知らせる（Callbacks::stateChanged）。known = false（ガイドの指定を書いていない
        古いアーカイブ）なら、戻し終えて最初に見た指定をホストの持っているものとする。新しい文書は空（known）。 */
    void setHostGuides (const std::map<juce::String, juce::String>& guides, const juce::String& guide, bool known);

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

    /** 保存に書くトラックごとのガイド: エンジンの最新に、まだ当てていない戻し途中の指定を重ねたもの。 */
    std::map<juce::String, juce::String> getGuidesForStore() const;

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
    /** between: 続きの再合成（more）の前に呼ぶ（保存の状態の変化を再合成の合間にも知らせる）。 */
    bool renderModification (const SyncModification&, bool stateChanged, juce::StringArray& contentChanged, juce::StringArray& caughtUp,
                             const std::function<void()>& between);
    /** ara_revs の保存の状態の署名・ガイドの指定を、ホストの持っているものと比べ、違えば Callbacks::stateChanged で知らせて
        ホストの持っているものを更新する（再合成の前と合間に呼ぶ）。changed: 保存用の写しを取り直す修飾に足す。 */
    void noticeSavedState (const SyncModel&, const juce::var& revs, juce::StringArray& changed);
    /** ara_set_modification・ara_restore の返り値の版（rev）・保存の状態の署名（state）を、ホストが持っている
        （開いた・戻した）ものとして覚える。restored: アーカイブから戻した（ホストの持っているものはそれ。登録し直しでは
        前に覚えた保存の状態を保つ）。エンジンが保存したときの音を出せない（render_changed。このエンジンより新しい描画の版）
        なら、最初の再合成も知らせる。 */
    void setOpenedEdits (const juce::String& araId, const juce::var& result, bool restored);
    /** ara_revs のガイドの指定を、保存に書く写しに入れる（refreshArchivesLocked と同じ。mutex を持って呼ぶ）。 */
    void takeGuidesLocked (const juce::var& guide, const juce::var& guides);
    /** 保存に書くガイドの指定がホストの持っているものと違ってきた修飾（違わなければ空。mutex を持たずに呼ぶ）。
        ready: 登録と戻しが済んでいる（ホストの持っているものが分からない古いアーカイブは、ここで覚える）。 */
    juce::StringArray guidesChangedSinceHost (const SyncModel&, bool ready);
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
    std::map<juce::String, juce::String> pendingGuides;   // 戻している途中のトラックごとのガイド（当てたものから外す）
    std::map<juce::String, juce::String> latestGuides;
    std::map<juce::String, juce::String> hostGuides;     // ホストが持っているガイドの指定（setHostGuides・知らせた後）
    juce::String hostGuide;
    bool hostGuidesKnown = true;
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
    std::map<juce::String, juce::String> openedEdits;       // ara_id → ホストの持っている編集の署名（開いた・戻した・音の変化を知らせた時）
    std::map<juce::String, juce::String> hostStates;        // ara_id → ホストの持っている保存の状態の署名（ara_revs の states）
    std::set<juce::String> renderChanged;                   // 保存したときの音を出せない編集を戻した修飾（最初の再合成も知らせる）
    std::set<juce::String> stateMoved;                      // 保存の状態の変化を知らせた後、まだ再合成していない修飾
    double lastStateCheck = 0.0;                            // 再合成の合間に保存の状態を見直した時刻
    std::map<juce::String, juce::String> notesRev;          // ara_id → ノートの写しを取ったときの ara_revs の版
    ExternalChanges external;                               // 外部の AI の中継の番号（ara_revs の external）

    McpClient mcp;   // 最後に置く（先に壊れて、終了の通知が上の原子変数より後に来ないように）
};

} // namespace gliss
