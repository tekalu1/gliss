// GlissARATest: Gliss の ARA プラグインの通し試験（エンジンにつないだ編集 → 再生 → アーカイブの往復）。
// ARA SDK の TestHost の部品（TestHost・ARADocumentController・CompanionAPIs・TestCases）を使い、main だけをこのファイルに差し替える。
//
//   GlissARATest -vst3 <Gliss.vst3> -out <フォルダ> -workB <作業場所 B> [-rate <描画 C の周波数>]
//   GlissARATest -vst3 <Gliss.vst3> -out <フォルダ> -relay <relay_client.py>   （外部の AI からの編集の通し。下の runRelay）
//   GlissARATest -vst3 <Gliss.vst3> -out <フォルダ> -changes <relay_client.py> -workB <別の作業場所>
//     （保存するものが変わるたびにホストへ知らせ、戻しただけでは知らせないことの通し。下の runChanges）
//
//   N. 合成の歌声もどき（44.1 kHz・モノラル・6.2 秒）を 1 つの AudioSource にし、リージョンを 2 つ（ソース全体をソングの 0 秒、
//      ソースの 1.5〜4.2 秒をソングの 10 秒）置いたドキュメントを、編集なし（GLISS_TEST_EDIT を消す）で作り、ホストとして解析を頼んで
//      終わるまで待つ → ソース・修飾・リージョンのノート（kARAContentTypeNotes）と品質のラベル → notes-n.json
//   A. 同じ音のドキュメントを作り、ソースの周波数で描画 → render-a.f32
//      （プラグインは環境変数 GLISS_TEST_EDIT の編集を当て、GLISS_ARA_SYNC_WAIT_MS の間、同期の完了を待ってから描く）。
//      その後、リージョンのノートが adjusted になるのを待つ → notes-a.json
//   保存（storeObjectsToArchive）→ archive.json
//   B. ドキュメントを閉じ（エンジンも止まる）、作業場所を -workB に替え、GLISS_TEST_EDIT を消して、同じ永続 ID でドキュメントを作り直し、
//      アーカイブを戻して描画 → render-b.f32（アーカイブだけから同じ音になるか）。戻した編集のノート → notes-b.json
//   C. 同じドキュメントを別の周波数（既定 48000 Hz）で描画 → render-c.f32（周波数の変換の経路）
//   D. 同じ作業場所（B）のまま、同じアーカイブからもう一度ドキュメントを作り直して描画し、同期が済むまで回す（曲を開き直しただけ）
//   B・D ではホストがノートを読む前まで、プラグインからホストへの「中身が変わった」の知らせ（ARAModelUpdateControllerInterface。
//   ModelUpdateCounts.h）が 1 つも無いこと（ARA は戻した状態と違うときだけ知らせる。Studio Pro は知らせを受けると曲を
//   「変更あり」にする）。D の終わりにはノートが読めている（知らせが無いのは、ノートが来なかったからではない）。数は updates.json。
//   -relay では反対に、外部の編集の後に修飾とリージョンの「音が変わった」が届くこと（updates-r.json）。
//   ソースの音 → source.f32、ID と長さ → summary.json。比べるのは plugin/tests/verify_ara_engine.py。
//
// notes-*.json は {sources: {id: 中身}, modifications: {id: 中身}, regions: [{song_start, mod_start, …, content: 中身}]}、
// 中身 = {available, grade, notes: [[frequency, pitchNumber, volume, startPosition, attackDuration, noteDuration, signalDuration]]}。
// 出力の .f32 はチャンネル 0 の float32（リトルエンディアン）。終了コード 0 = 最後まで流れた（音の正しさは verify が見る）。

#include "TestCases.h"
#include "TestHost.h"
#include "ModelUpdateCounts.h"
#include "ARAHostInterfaces/ARAAudioAccessController.h"

#include "ARA_Library/Utilities/ARASamplePositionConversion.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#if defined (_WIN32)
    #include <windows.h>
#endif

ARA::ARAAssertFunction assertFunction { &ARA::ARAInterfaceAssert };
ARA::ARAAssertFunction* assertFunctionReference { &assertFunction };

ARA_SETUP_DEBUG_MESSAGE_PREFIX ("GlissARATest");

// TestCases.cpp にある（ヘッダには出ていない）
ARADocumentController* createHostAndBasicDocument (PlugInEntry* plugInEntry, std::unique_ptr<TestHost>& testHost, std::string documentName,
                                                   bool requestPlugInAnalysisAndBlock, const AudioFileList& audioFiles);

namespace
{
const std::string sourceID { "audioSourceTestPersistentID 0" };          // createHostAndBasicDocument が付ける ID
const std::string modificationID { "audioModificationTestPersistentID 0" };
const std::string secondModificationID { "audioModificationTestPersistentID 1" };   // -changes の 2 つ目の修飾

/** 合成の歌声もどき（engine/tests/test_ara_tools.py の _voice と同じ作り: 倍音 8 本・5.5 Hz ±30 セントのビブラート・5 ノート）。 */
class VoiceAudioFile : public AudioFileBase
{
public:
    explicit VoiceAudioFile (double sampleRate) : AudioFileBase { "Gliss test voice" }, _sampleRate { sampleRate }
    {
        struct Note { double start, length; int midi; };
        const Note notes[] { { 0.5, 0.55, 60 }, { 1.65, 0.55, 64 }, { 2.8, 0.55, 67 }, { 3.95, 0.55, 65 }, { 5.1, 0.55, 62 } };
        const auto count { static_cast<size_t> (6.2 * sampleRate) };
        _samples.assign (count, 0.0f);

        for (const auto& n : notes)
        {
            const auto a { static_cast<size_t> (n.start * sampleRate) }, b { static_cast<size_t> ((n.start + n.length) * sampleRate) };
            const auto f0 { 440.0 * std::pow (2.0, (n.midi - 69) / 12.0) };
            double phase { 0.0 };

            for (size_t i { a }; i < b && i < count; ++i)
            {
                const auto t { static_cast<double> (i - a) / sampleRate };
                const auto vib { 30.0 * std::sin (2.0 * M_PI * 5.5 * t) * std::min (1.0, std::max (0.0, (t - 0.12) / 0.1)) };
                phase += 2.0 * M_PI * f0 * std::pow (2.0, vib / 1200.0) / sampleRate;
                double s { 0.0 };
                for (int k { 0 }; k < 8; ++k)
                    s += std::pow (0.6, k) * std::sin ((k + 1) * phase);
                const auto env { std::min (1.0, std::min (t / 0.03, (n.length - t) / 0.04)) };
                _samples[i] += static_cast<float> (0.25 * s * env);
            }
        }

        // 決まった種の小さな雑音（無音の区間を真っ平にしない）
        uint32_t seed { 12345u };
        for (auto& v : _samples)
        {
            seed = seed * 1664525u + 1013904223u;
            v += static_cast<float> ((static_cast<double> (seed >> 8) / 16777216.0 - 0.5) * 2.0e-4);
        }
    }

    int64_t getSampleCount () const noexcept override { return static_cast<int64_t> (_samples.size ()); }
    double getSampleRate () const noexcept override { return _sampleRate; }
    int getChannelCount () const noexcept override { return 1; }
    bool merits64BitSamples () const noexcept override { return false; }

    bool readSamples (int64_t samplePosition, int64_t samplesPerChannel, void* const buffers[], bool use64BitSamples) noexcept override
    {
        for (int64_t i { 0 }; i < samplesPerChannel; ++i)
        {
            const auto p { samplePosition + i };
            const auto v { (p >= 0 && p < getSampleCount ()) ? _samples[static_cast<size_t> (p)] : 0.0f };
            if (use64BitSamples)
                static_cast<double*> (buffers[0])[i] = v;
            else
                static_cast<float*> (buffers[0])[i] = v;
        }
        return true;
    }

    bool saveToFile (const std::string&) override { return false; }

    const std::vector<float>& samples () const noexcept { return _samples; }

private:
    double _sampleRate;
    std::vector<float> _samples;
};

bool writeFloats (const std::string& path, const std::vector<float>& data)
{
    std::ofstream out (path, std::ios::binary);
    out.write (reinterpret_cast<const char*> (data.data ()), static_cast<std::streamsize> (data.size () * sizeof (float)));
    return out.good ();
}

void setEnv (const char* name, const char* value)
{
#if defined (_WIN32)
    SetEnvironmentVariableA (name, value);
#else
    if (value != nullptr) setenv (name, value, 1); else unsetenv (name);
#endif
}

/** ホストのメインスレッドの仕事を ms の間回す: プラグインのメッセージ（JUCE の callAsync。プラグインはそこでホストへの知らせを
    積む）を配り、積まれた知らせを受け取る（notifyModelUpdates）。idleThreadForDuration は眠るだけで、メッセージを配らない。 */
void pumpHost (PlugInEntry* plugInEntry, ARADocumentController* dc, int ms)
{
    for (int waited { 0 }; waited < ms; waited += 20)
    {
#if defined (_WIN32)
        MSG msg;
        while (PeekMessageW (&msg, nullptr, 0, 0, PM_REMOVE))
        {
            TranslateMessage (&msg);
            DispatchMessageW (&msg);
        }
#endif
        dc->getDocumentController ()->notifyModelUpdates ();
        plugInEntry->idleThreadForDuration (20);
    }
}

/** ドキュメントの全リージョンを PlaybackRenderer の役のインスタンスで描く（TestCases.cpp の testPlaybackRendering と同じ流し方）。
    チャンネル 0 を返す。 */
std::vector<float> renderDocument (PlugInEntry* plugInEntry, ARADocumentController* araDocumentController, double renderSampleRate)
{
    const auto document { araDocumentController->getDocument () };
    auto plugInInstance { plugInEntry->createPlugInInstance () };
    plugInInstance->bindToDocumentControllerWithRoles (araDocumentController->getDocumentController ()->getRef (), ARA::kARAPlaybackRendererRole);
    auto playbackRenderer { plugInInstance->getPlaybackRenderer () };

    double start { 1.0e9 }, end { -1.0e9 };
    for (const auto& regionSequence : document->getRegionSequences ())
        for (const auto& playbackRegion : regionSequence->getPlaybackRegions ())
        {
            playbackRenderer.addPlaybackRegion (araDocumentController->getRef (playbackRegion));
            start = std::min (start, playbackRegion->getStartInPlaybackTime ());
            end = std::max (end, playbackRegion->getStartInPlaybackTime () + playbackRegion->getDurationInPlaybackTime ());
        }

    const auto first { ARA::samplePositionAtTime (start, renderSampleRate) };
    const auto last { ARA::samplePositionAtTime (end, renderSampleRate) };
    const auto sampleCount { static_cast<size_t> (last - first) };
    constexpr int channelCount { 1 };
    constexpr int blockSize { 2048 };
    std::vector<float> output (sampleCount, 0.0f);

    plugInInstance->startRendering (channelCount, blockSize, renderSampleRate);

    bool done { false };
    std::thread renderThread { [&] ()
    {
        ARAAudioAccessController::registerRenderThread ();
        for (auto position { first }; position < last; position += blockSize)
        {
            const auto n { std::min (blockSize, static_cast<int> (last - position)) };
            float* buffers[] { output.data () + (position - first) };
            plugInInstance->renderSamples (n, position, buffers);
        }
        ARAAudioAccessController::unregisterRenderThread ();
        done = true;
    } };

    while (! done)
        plugInEntry->idleThreadForDuration (10);
    renderThread.join ();

    plugInInstance->stopRendering ();
    for (const auto& regionSequence : document->getRegionSequences ())
        for (const auto& playbackRegion : regionSequence->getPlaybackRegions ())
            playbackRenderer.removePlaybackRegion (araDocumentController->getRef (playbackRegion));

    ARA_LOG ("rendered %zu samples at %g Hz", output.size (), renderSampleRate);
    return output;
}

std::string argument (const std::vector<std::string>& args, const std::string& name, const std::string& fallback = {})
{
    for (size_t i { 0 }; i + 1 < args.size (); ++i)
        if (args[i] == name)
            return args[i + 1];
    return fallback;
}

/** 1 つのオブジェクトのノート（kARAContentTypeNotes）を JSON に: {available, grade, notes: [[frequency, pitchNumber, volume,
    startPosition, attackDuration, noteDuration, signalDuration], ...]}。 */
std::string describeNotes (ARA::Host::DocumentController* hc, bool available, ARA::ARAContentGrade grade,
                           const std::function<ARA::ARAContentReaderRef ()>& createReader)
{
    std::ostringstream o;
    o.precision (17);
    o << "{\"available\": " << (available ? "true" : "false") << ", \"grade\": " << static_cast<int> (grade) << ", \"notes\": [";

    if (available)
    {
        const auto reader { createReader () };
        const auto count { hc->getContentReaderEventCount (reader) };

        for (ARA::ARAInt32 i { 0 }; i < count; ++i)
        {
            const auto* n { static_cast<const ARA::ARAContentNote*> (hc->getContentReaderDataForEvent (reader, i)) };
            o << (i > 0 ? ", " : "") << "[" << static_cast<double> (n->frequency) << ", " << n->pitchNumber << ", "
              << static_cast<double> (n->volume) << ", " << n->startPosition << ", " << n->attackDuration << ", "
              << n->noteDuration << ", " << n->signalDuration << "]";
        }

        hc->destroyContentReader (reader);
    }

    o << "]}";
    return o.str ();
}

/** ドキュメントの全部（ソース・修飾・リージョン）のノートを JSON に。 */
std::string describeDocumentNotes (ARADocumentController* dc)
{
    constexpr auto notes { ARA::kARAContentTypeNotes };
    auto* hc { dc->getDocumentController () };
    std::ostringstream o;
    o.precision (17);
    o << "{\"sources\": {";
    std::string mods, regions;
    bool firstSource { true };

    for (const auto& source : dc->getDocument ()->getAudioSources ())
    {
        const auto s { dc->getRef (source.get ()) };
        o << (firstSource ? "" : ", ") << "\"" << source->getPersistentID () << "\": "
          << describeNotes (hc, hc->isAudioSourceContentAvailable (s, notes), hc->getAudioSourceContentGrade (s, notes),
                            [&] { return hc->createAudioSourceContentReader (s, notes, nullptr); });
        firstSource = false;

        for (const auto& modification : source->getAudioModifications ())
        {
            const auto m { dc->getRef (modification.get ()) };
            mods += (mods.empty () ? "" : ", ") + ("\"" + modification->getPersistentID () + "\": ")
                    + describeNotes (hc, hc->isAudioModificationContentAvailable (m, notes), hc->getAudioModificationContentGrade (m, notes),
                                     [&] { return hc->createAudioModificationContentReader (m, notes, nullptr); });

            for (const auto& region : modification->getPlaybackRegions ())
            {
                const auto r { dc->getRef (region.get ()) };
                std::ostringstream ro;
                ro.precision (17);
                ro << "{\"modification\": \"" << modification->getPersistentID () << "\", \"song_start\": " << region->getStartInPlaybackTime ()
                   << ", \"song_duration\": " << region->getDurationInPlaybackTime () << ", \"mod_start\": " << region->getStartInModificationTime ()
                   << ", \"mod_duration\": " << region->getDurationInModificationTime () << ", \"content\": "
                   << describeNotes (hc, hc->isPlaybackRegionContentAvailable (r, notes), hc->getPlaybackRegionContentGrade (r, notes),
                                     [&] { return hc->createPlaybackRegionContentReader (r, notes, nullptr); })
                   << "}";
                regions += (regions.empty () ? "" : ", ") + ro.str ();
            }
        }
    }

    o << "}, \"modifications\": {" << mods << "}, \"regions\": [" << regions << "]}";
    return o.str ();
}

/** 全部のリージョンのノートが読めて、品質のラベルが want になるまで待つ（同期のスレッドがエンジンから取る）。 */
bool waitForRegionNotes (PlugInEntry* plugInEntry, ARADocumentController* dc, ARA::ARAContentGrade want, int timeoutMs)
{
    auto* hc { dc->getDocumentController () };

    for (int waited { 0 }; waited < timeoutMs; waited += 50)
    {
        bool all { true };

        for (const auto& source : dc->getDocument ()->getAudioSources ())
            for (const auto& modification : source->getAudioModifications ())
                for (const auto& region : modification->getPlaybackRegions ())
                {
                    const auto r { dc->getRef (region.get ()) };
                    all = all && hc->isPlaybackRegionContentAvailable (r, ARA::kARAContentTypeNotes)
                          && hc->getPlaybackRegionContentGrade (r, ARA::kARAContentTypeNotes) == want;
                }

        if (all)
            return true;

        pumpHost (plugInEntry, dc, 50);
    }

    ARA_LOG ("the notes did not reach grade %i in %i ms", static_cast<int> (want), timeoutMs);
    return false;
}

bool writeText (const std::string& path, const std::string& text)
{
    std::ofstream out (path, std::ios::binary);
    out << text << "\n";
    return out.good ();
}

#if defined (_WIN32)
std::wstring widen (const std::string& s)
{
    if (s.empty ())
        return {};
    const auto n { MultiByteToWideChar (CP_UTF8, 0, s.data (), static_cast<int> (s.size ()), nullptr, 0) };
    std::wstring w (static_cast<size_t> (n), L'\0');
    MultiByteToWideChar (CP_UTF8, 0, s.data (), static_cast<int> (s.size ()), w.data (), n);
    return w;
}

/** 外部の AI の代わり（relay_client.py）を起動し、終わるまで待つ（その間もホストのメインスレッドの仕事を回す）。終了コードを返す。
    step: relay_client.py の 2 つ目の引数（空なら渡さない）。 */
int runExternalClient (PlugInEntry* plugInEntry, const std::string& script, const std::string& outDir, int timeoutMs,
                       const std::string& step = {})
{
    const auto* python { std::getenv ("GLISS_ENGINE_PYTHON") };
    const auto* cwd { std::getenv ("GLISS_ENGINE_CWD") };
    if (python == nullptr || cwd == nullptr)
    {
        ARA_LOG ("relay: GLISS_ENGINE_PYTHON and GLISS_ENGINE_CWD are required");
        return -1;
    }

    auto commandLine { L"\"" + widen (python) + L"\" \"" + widen (script) + L"\" \"" + widen (outDir) + L"\""
                       + (step.empty () ? std::wstring () : L" " + widen (step)) };
    const auto workDir { widen (cwd) };
    STARTUPINFOW si {};
    si.cb = sizeof (si);
    PROCESS_INFORMATION pi {};
    if (! CreateProcessW (nullptr, commandLine.data (), nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, workDir.c_str (), &si, &pi))
    {
        ARA_LOG ("relay: could not start the external client (%lu)", GetLastError ());
        return -1;
    }

    CloseHandle (pi.hThread);
    DWORD code { STILL_ACTIVE };
    for (int waited { 0 }; waited < timeoutMs; waited += 50)
    {
        if (WaitForSingleObject (pi.hProcess, 0) == WAIT_OBJECT_0)
            break;
        plugInEntry->idleThreadForDuration (50);
    }

    if (WaitForSingleObject (pi.hProcess, 0) != WAIT_OBJECT_0)
    {
        ARA_LOG ("relay: the external client did not finish in %i ms", timeoutMs);
        TerminateProcess (pi.hProcess, 1);
        WaitForSingleObject (pi.hProcess, 5000);
    }

    GetExitCodeProcess (pi.hProcess, &code);
    CloseHandle (pi.hProcess);
    return static_cast<int> (code);
}
#endif

/** -relay: 外部の AI（別のプロセスの vocal_engine.mcp）から、開いているドキュメントの修飾を編集する通し。
    R0. ドキュメントを作って編集なしで描画 → render-r0.f32（原音のはず）
    外部のクライアント（relay_client.py）が ara_documents → ara_attach → analyze_take → shift_pitch（+100 セント）→ relay-client.json
    R1. プラグインが 1 秒ごとの ara_revs で拾うのを、ノートが adjusted になるまで待って描画 → render-r1.f32
    保存 → archive-r.json（外部の編集が DAW のソングに入るか）。比べるのは plugin/tests/verify_ara_relay.py。 */
int runRelay (PlugInEntry* plugInEntry, const AudioFileList& files, const VoiceAudioFile& voice,
              const ARA::ARAFactory* factory, const std::string& script, const std::string& outDir)
{
#if defined (_WIN32)
    setEnv ("GLISS_TEST_EDIT", nullptr);
    MemoryArchive archive { factory->documentArchiveID };
    std::vector<float> render0, render1;
    int clientCode { -1 };
    {
        std::unique_ptr<TestHost> testHost;
        auto dc { createHostAndBasicDocument (plugInEntry, testHost, "GlissARATest R", false, files) };
        render0 = renderDocument (plugInEntry, dc, voice.getSampleRate ());

        pumpHost (plugInEntry, dc, 2000);           // 作った直後の知らせを流してから数える
        modelUpdateCounts () = {};
        clientCode = runExternalClient (plugInEntry, script, outDir, 300000);
        ARA_LOG ("relay: the external client exited with %i", clientCode);
        if (clientCode != 0)
            return 1;

        // 外部の編集は ara_revs（1 秒ごと）で拾われる: 編集の後のノート（adjusted）になるまで待ってから描く
        if (! waitForRegionNotes (plugInEntry, dc, ARA::kARAContentGradeAdjusted, 60000))
            return 1;
        render1 = renderDocument (plugInEntry, dc, voice.getSampleRate ());
        writeText (outDir + "/notes-r.json", describeDocumentNotes (dc));

        // 外部の編集で音が変わった: ホストに知らせている（下の D で知らせが無いことの裏返し。数える仕掛けが働いている）
        pumpHost (plugInEntry, dc, 2000);
        const auto counts { modelUpdateCounts () };
        writeText (outDir + "/updates-r.json", counts.toJson ());
        if (counts.modificationSamples == 0 || counts.regionSamples == 0)
        {
            ARA_LOG ("relay: the host was not told that the edited modification changed: %s", counts.toJson ().c_str ());
            return 1;
        }

        if (! dc->supportsPartialPersistency () || ! dc->storeObjectsToArchive (&archive))
        {
            ARA_LOG ("storing the archive failed");
            return 1;
        }
    }

    const std::string archiveData = archive;
    {
        std::ofstream out (outDir + "/archive-r.json", std::ios::binary);
        out << archiveData;
    }
    writeFloats (outDir + "/render-r0.f32", render0);
    writeFloats (outDir + "/render-r1.f32", render1);
    {
        std::ofstream out (outDir + "/summary-r.json", std::ios::binary);
        out << "{\"source_id\": \"" << sourceID << "\", \"modification_id\": \"" << modificationID << "\", "
            << "\"source_rate\": " << voice.getSampleRate () << ", \"source_frames\": " << voice.getSampleCount () << ", "
            << "\"frames_r0\": " << render0.size () << ", \"frames_r1\": " << render1.size () << "}\n";
    }
    plugInEntry->uninitializeARA ();
    ARA_LOG ("relay: done");
    return 0;
#else
    ARA_LOG ("-relay is only implemented on Windows");
    return 2;
#endif
}

#if defined (_WIN32)
/** -changes の文書: 1 つのソースに修飾 2 つ（別の DAW のトラック。どちらもソース全体をソングの 0 秒に）。archive があれば戻す。 */
ARADocumentController* createTwoModificationDocument (PlugInEntry* plugInEntry, std::unique_ptr<TestHost>& testHost,
                                                      const std::string& name, VoiceAudioFile* voice, MemoryArchive* archive)
{
    testHost = std::make_unique<TestHost> ();
    auto document { testHost->addDocument (name, plugInEntry) };
    auto dc { testHost->getDocumentController (document) };

    dc->beginEditing ();
    auto musicalContext { testHost->addMusicalContext (document, "ARA Test Musical Context", { 1.0f, 0.0f, 0.0f }) };
    auto sequence0 { testHost->addRegionSequence (document, "Track 1", musicalContext, { 0.0f, 1.0f, 0.0f }) };
    auto sequence1 { testHost->addRegionSequence (document, "Track 2", musicalContext, { 0.0f, 1.0f, 0.0f }) };
    auto audioSource { testHost->addAudioSource (document, voice, sourceID) };
    dc->enableAudioSourceSamplesAccess (audioSource, true);
    auto mod0 { testHost->addAudioModification (document, audioSource, "Test audio modification 0", modificationID) };
    auto mod1 { testHost->addAudioModification (document, audioSource, "Test audio modification 1", secondModificationID) };
    testHost->addPlaybackRegion (document, mod0, ARA::kARAPlaybackTransformationNoChanges, 0.0, audioSource->getDuration (),
                                 0.0, audioSource->getDuration (), sequence0, "Test playback region", { 0.0f, 0.0f, 1.0f });
    testHost->addPlaybackRegion (document, mod1, ARA::kARAPlaybackTransformationNoChanges, 0.0, audioSource->getDuration (),
                                 0.0, audioSource->getDuration (), sequence1, "Second playback region", { 0.0f, 0.0f, 1.0f });
    const auto restored { archive == nullptr || dc->restoreObjectsFromArchive (archive) };
    dc->endEditing ();
    return restored ? dc : nullptr;
}

/** 画面の代わり（プラグインの GLISS_TEST_BRIDGE_DIR）: {tool, args} を書いて、プラグインが engineCall に渡した答えを待つ。
    args の "@ara:<persistentID>" はプラグインがトラックの id に置き換える。ok:false なら false。 */
struct TestBridgeCalls
{
    std::string dir;
    int serial = 0;

    bool call (PlugInEntry* plugInEntry, ARADocumentController* dc, const std::string& tool, const std::string& argsJson)
    {
        char name[16];
        std::snprintf (name, sizeof (name), "%03d", ++serial);
        const auto base { dir + "/" + name };
        writeText (base + ".call.tmp", "{\"tool\": \"" + tool + "\", \"args\": " + argsJson + "}");
        std::rename ((base + ".call.tmp").c_str (), (base + ".call.json").c_str ());

        for (int waited { 0 }; waited < 300000; waited += 50)
        {
            std::ifstream in (base + ".result.json", std::ios::binary);
            if (in)
            {
                std::stringstream text;
                text << in.rdbuf ();
                const auto result { text.str () };
                const bool ok { result.find ("\"ok\": false") == std::string::npos };
                ARA_LOG ("bridge: %s %s -> %s", tool.c_str (), argsJson.c_str (), ok ? "ok" : result.substr (0, 300).c_str ());
                return ok;
            }
            pumpHost (plugInEntry, dc, 50);
        }

        ARA_LOG ("bridge: %s did not answer", tool.c_str ());
        return false;
    }
};

/** 知らせが done を満たすまで（最長 timeoutMs）ホストを回し、満たしたらもう少し回して残りの知らせも受ける。 */
bool waitForUpdates (PlugInEntry* plugInEntry, ARADocumentController* dc, const std::function<bool (const ModelUpdateCounts&)>& done,
                     int timeoutMs)
{
    for (int waited { 0 }; waited < timeoutMs; waited += 100)
    {
        if (done (modelUpdateCounts ()))
        {
            pumpHost (plugInEntry, dc, 2000);
            return true;
        }
        pumpHost (plugInEntry, dc, 100);
    }
    return done (modelUpdateCounts ());
}

/** -changes: 保存するもの（ARA のアーカイブ）が変わる操作のたびに、ホストへ「変わった」の知らせが届くこと（音が変わらない
    ものは、音もノートも変わらない知らせ = modification_state、ガイドの指定は文書の知らせ = document_data も）と、
    保存した文書を戻しただけでは何も届かないことを確かめる。結果は changes.json（操作ごとの知らせの数と合否）。
    画面の操作はプラグインの試験用の口（GLISS_TEST_BRIDGE_DIR。画面と同じ engineCall）、外部の AI は relay_client.py。
      1. 画面: 修飾 0 を +100 セント・ピッチ曲線（音が変わる）
      2. 画面: 修飾 1 の歌詞だけ（音も編集も無い）
      3. 画面: 修飾 1 の F0 の方式だけ（編集の無い修飾。scope = current）
      4. 画面: 修飾 0 のガイドを修飾 1 に（トラックごとのガイド）
      5〜8. 画面: 取り消し（ガイド → 方式）・やり直し（方式 → ガイド）
      9. 外部の AI: 修飾 0 を -50 セント（音が変わる）
      10. 外部の AI: 修飾 1 のガイドを修飾 0 に
      11. 画面: 編集対象の切り替え・一覧だけ（保存するものは変わらない: 何も届かない）
      保存 → 12. 同じ作業場所で戻す（何も届かない）・13. 別の作業場所で戻す（解析し直しても何も届かない）
      14. 描画の版を 1（0.1.0-beta.6 までの曲）にしたアーカイブを戻す（その版の音のまま鳴らす: 何も届かない）
      15. その文書で、画面から描画の版を上げる（set_render_version。保存するものが変わる: 知らせが届く） */
int runChanges (PlugInEntry* plugInEntry, VoiceAudioFile& voice, const ARA::ARAFactory* factory,
                const std::string& script, const std::string& outDir, const std::string& workOther)
{
    setEnv ("GLISS_TEST_EDIT", nullptr);
    TestBridgeCalls bridge { outDir + "/bridge" };
    CreateDirectoryW (widen (bridge.dir).c_str (), nullptr);
    setEnv ("GLISS_TEST_BRIDGE_DIR", bridge.dir.c_str ());

    std::string report;
    bool allPassed { true };
    const auto record = [&] (const std::string& name, bool passed)
    {
        const auto counts { modelUpdateCounts () };
        report += (report.empty () ? "" : ",\n ") + ("\"" + name + "\": {\"pass\": " + (passed ? "true" : "false")
                                                    + ", \"counts\": " + counts.toJson () + "}");
        ARA_LOG ("changes: %s %s %s", name.c_str (), passed ? "PASS" : "FAIL", counts.toJson ().c_str ());
        allPassed = allPassed && passed;
    };
    const auto samples = [] (const ModelUpdateCounts& c) { return c.modificationSamples > 0 && c.regionSamples > 0; };
    const auto stateOnly = [] (const ModelUpdateCounts& c) { return c.modificationState > 0; };
    // 方式の切り替えは解析し直したノートの知らせ（notesAreAffected）と同じ周に来ると、ARA の知らせの束ね（範囲の和）で
    // 1 つの「ノートが変わった」になる（ホストはどちらでも保存し直す）。音の知らせではないことだけを見る
    const auto notSamples = [] (const ModelUpdateCounts& c) { return c.modificationOther > 0; };
    const auto documentData = [] (const ModelUpdateCounts& c) { return c.documentData > 0 && c.modificationState > 0; };
    const std::string m0 { "\"@ara:" + modificationID + "\"" }, m1 { "\"@ara:" + secondModificationID + "\"" };
    MemoryArchive archive { factory->documentArchiveID };

    {
        std::unique_ptr<TestHost> testHost;
        auto dc { createTwoModificationDocument (plugInEntry, testHost, "GlissARATest X", &voice, nullptr) };
        renderDocument (plugInEntry, dc, voice.getSampleRate ());
        waitForRegionNotes (plugInEntry, dc, ARA::kARAContentGradeDetected, 60000);
        pumpHost (plugInEntry, dc, 2000);

        bool called { bridge.call (plugInEntry, dc, "select_track", "{\"track_id\": " + m0 + "}")
                      && bridge.call (plugInEntry, dc, "analyze_take", "{\"background\": false}") };
        pumpHost (plugInEntry, dc, 2000);
        modelUpdateCounts () = {};
        called = called && bridge.call (plugInEntry, dc, "shift_pitch", "{\"cents\": 100, \"start_sec\": 0, \"end_sec\": 6.2}")
                 && bridge.call (plugInEntry, dc, "set_pitch_curve",
                                 "{\"points\": [[0, 0], [0.2, 80], [0.4, 0]], \"start_sec\": 1.65, \"end_sec\": 2.2}");
        record ("1 screen edit", called && waitForUpdates (plugInEntry, dc, samples, 60000));

        called = bridge.call (plugInEntry, dc, "select_track", "{\"track_id\": " + m1 + "}")
                 && bridge.call (plugInEntry, dc, "analyze_take", "{\"background\": false}");
        pumpHost (plugInEntry, dc, 2000);
        modelUpdateCounts () = {};
        called = called && bridge.call (plugInEntry, dc, "set_lyrics",
                                        "{\"text\": \"\u3042\u3044\u3046\u3048\u304a\", \"start_sec\": 0.4, \"end_sec\": 6.0, \"reanalyze\": false}");
        record ("2 screen lyrics only", called && waitForUpdates (plugInEntry, dc, stateOnly, 30000)
                                        && modelUpdateCounts ().modificationSamples == 0);

        modelUpdateCounts () = {};
        called = bridge.call (plugInEntry, dc, "set_f0_estimator", "{\"estimator\": \"gliss\", \"scope\": \"current\"}");
        record ("3 screen estimator only", called && waitForUpdates (plugInEntry, dc, notSamples, 30000)
                                           && modelUpdateCounts ().modificationSamples == 0);

        modelUpdateCounts () = {};
        called = bridge.call (plugInEntry, dc, "set_track_guide", "{\"track_id\": " + m0 + ", \"guide_track_id\": " + m1 + "}");
        record ("4 screen guide", called && waitForUpdates (plugInEntry, dc, documentData, 30000));

        modelUpdateCounts () = {};
        called = bridge.call (plugInEntry, dc, "undo", "{}");
        record ("5 screen undo (guide)", called && waitForUpdates (plugInEntry, dc, documentData, 30000));

        modelUpdateCounts () = {};
        called = bridge.call (plugInEntry, dc, "undo", "{}");
        record ("6 screen undo (estimator)", called && waitForUpdates (plugInEntry, dc, notSamples, 30000));

        modelUpdateCounts () = {};
        called = bridge.call (plugInEntry, dc, "redo", "{}");
        record ("7 screen redo (estimator)", called && waitForUpdates (plugInEntry, dc, notSamples, 30000));

        modelUpdateCounts () = {};
        called = bridge.call (plugInEntry, dc, "redo", "{}");
        record ("8 screen redo (guide)", called && waitForUpdates (plugInEntry, dc, documentData, 30000));

        modelUpdateCounts () = {};
        called = runExternalClient (plugInEntry, script, outDir, 300000, "edit") == 0;
        record ("9 external edit", called && waitForUpdates (plugInEntry, dc, samples, 60000));

        modelUpdateCounts () = {};
        called = runExternalClient (plugInEntry, script, outDir, 300000, "guide") == 0;
        record ("10 external guide", called && waitForUpdates (plugInEntry, dc, documentData, 30000));

        pumpHost (plugInEntry, dc, 2000);
        modelUpdateCounts () = {};
        called = bridge.call (plugInEntry, dc, "select_track", "{\"track_id\": " + m0 + "}")
                 && bridge.call (plugInEntry, dc, "list_tracks", "{}");
        pumpHost (plugInEntry, dc, 5000);
        record ("11 selection only (nothing)", called && modelUpdateCounts ().changes () == 0);

        if (! dc->supportsPartialPersistency () || ! dc->storeObjectsToArchive (&archive))
        {
            ARA_LOG ("storing the archive failed");
            return 1;
        }
    }

    const std::string archiveData = archive;
    writeText (outDir + "/archive-x.json", archiveData);
    const auto restoreOnly = [&] (const std::string& name, MemoryArchive& from, const std::function<bool (const ModelUpdateCounts&)>& pass)
    {
        modelUpdateCounts () = {};
        std::unique_ptr<TestHost> testHost;
        auto dc { createTwoModificationDocument (plugInEntry, testHost, "GlissARATest X " + name, &voice, &from) };
        if (dc == nullptr)
        {
            record (name, false);
            return;
        }
        renderDocument (plugInEntry, dc, voice.getSampleRate ());
        pumpHost (plugInEntry, dc, 8000);
        record (name, pass (modelUpdateCounts ()));
    };
    const auto nothing = [] (const ModelUpdateCounts& c) { return c.changes () == 0; };

    MemoryArchive same { archiveData, factory->documentArchiveID };
    restoreOnly ("12 restore in the same work folder (nothing)", same, nothing);

    // 描画の版を前の版にしたアーカイブ（0.1.0-beta.6 までの曲。その版の音のまま鳴らす）
    auto older { archiveData };
    const std::string current { "\"render_version\": 2" }, previous { "\"render_version\": 1" };
    int replaced { 0 };
    for (auto at { older.find (current) }; at != std::string::npos; at = older.find (current, at + previous.size ()), ++replaced)
        older.replace (at, current.size (), previous);
    MemoryArchive olderArchive { older, factory->documentArchiveID };
    {
        modelUpdateCounts () = {};
        std::unique_ptr<TestHost> testHost;
        auto dc { createTwoModificationDocument (plugInEntry, testHost, "GlissARATest X 14", &voice, &olderArchive) };
        if (dc == nullptr)
        {
            record ("14 restore an archive of the older renderer (nothing)", false);
        }
        else
        {
            renderDocument (plugInEntry, dc, voice.getSampleRate ());
            pumpHost (plugInEntry, dc, 8000);
            record ("14 restore an archive of the older renderer (nothing)", replaced > 0 && nothing (modelUpdateCounts ()));

            modelUpdateCounts () = {};
            const bool called { bridge.call (plugInEntry, dc, "select_track", "{\"track_id\": " + m0 + "}")
                                && bridge.call (plugInEntry, dc, "set_render_version", "{\"apply\": true}") };
            record ("15 screen upgrade of the renderer", called && waitForUpdates (plugInEntry, dc, [] (const ModelUpdateCounts& c)
                    { return c.modificationSamples > 0 || c.modificationState > 0; }, 30000));
        }
    }

    setEnv ("VOCAL_ENGINE_WORK_DIR", workOther.c_str ());
    MemoryArchive other { archiveData, factory->documentArchiveID };
    restoreOnly ("13 restore in another work folder (nothing)", other, nothing);

    writeText (outDir + "/changes.json", "{" + report + "}");
    plugInEntry->uninitializeARA ();
    ARA_LOG ("changes: %s", allPassed ? "all passed" : "FAILED");
    return allPassed ? 0 : 1;
}
#endif
} // namespace

int main (int argc, const char* argv[])
{
    const std::vector<std::string> args (argv, argv + argc);
    ARA::ARASetExternalAssertReference (assertFunctionReference);

    const auto outDir { argument (args, "-out") };
    const auto workB { argument (args, "-workB") };
    const auto relayScript { argument (args, "-relay") };
    const auto changesScript { argument (args, "-changes") };
    const auto otherRate { std::atof (argument (args, "-rate", "48000").c_str ()) };

    auto plugInEntry { PlugInEntry::parsePlugInEntry (args) };
    if (! plugInEntry || outDir.empty () || (workB.empty () && relayScript.empty ()) || (! changesScript.empty () && workB.empty ()))
    {
        ARA_LOG ("usage: GlissARATest -vst3 <Gliss.vst3> -out <folder> (-workB <work folder for document B> [-rate <Hz>] | -relay <relay_client.py>"
                 " | -changes <relay_client.py> -workB <another work folder>)");
        return 2;
    }

    const auto factory { plugInEntry->getARAFactory () };
    if (! factory)
    {
        ARA_LOG ("not an ARA plug-in");
        return 2;
    }

    plugInEntry->initializeARA (assertFunctionReference);

    auto voice { std::make_shared<VoiceAudioFile> (44100.0) };
    AudioFileList files { voice };
    writeFloats (outDir + "/source.f32", voice->samples ());

    if (! relayScript.empty ())
        return runRelay (plugInEntry.get (), files, *voice, factory, relayScript, outDir);

#if defined (_WIN32)
    if (! changesScript.empty ())
        return runChanges (plugInEntry.get (), *voice, factory, changesScript, outDir, workB);
#endif

    // ---- N: 編集なしで、ホストが解析を頼んで待つ → ノート（detected）。リージョンは 2 つ（2 つ目はソースの途中を別の位置に） ----
    const std::string testEdit { std::getenv ("GLISS_TEST_EDIT") != nullptr ? std::getenv ("GLISS_TEST_EDIT") : "" };
    setEnv ("GLISS_TEST_EDIT", nullptr);
    {
        if (factory->analyzeableContentTypesCount == 0)
        {
            ARA_LOG ("the plug-in names no analyzable content types");
            return 1;
        }

        auto testHost { std::make_unique<TestHost> () };
        auto document { testHost->addDocument ("GlissARATest N", plugInEntry.get ()) };
        auto dc { testHost->getDocumentController (document) };

        dc->beginEditing ();
        auto musicalContext { testHost->addMusicalContext (document, "ARA Test Musical Context", { 1.0f, 0.0f, 0.0f }) };
        auto regionSequence { testHost->addRegionSequence (document, "Track 1", musicalContext, { 0.0f, 1.0f, 0.0f }) };
        auto audioSource { testHost->addAudioSource (document, voice.get (), sourceID) };
        auto audioModification { testHost->addAudioModification (document, audioSource, "Test audio modification 0", modificationID) };
        testHost->addPlaybackRegion (document, audioModification, ARA::kARAPlaybackTransformationNoChanges,
                                     0.0, audioSource->getDuration (), 0.0, audioSource->getDuration (),
                                     regionSequence, "Test playback region", { 0.0f, 0.0f, 1.0f });
        testHost->addPlaybackRegion (document, audioModification, ARA::kARAPlaybackTransformationNoChanges,
                                     1.5, 2.7, 10.0, 2.7, regionSequence, "Trimmed playback region", { 0.0f, 0.0f, 1.0f });
        dc->endEditing ();
        dc->enableAudioSourceSamplesAccess (audioSource, true);

        // TestHost の requestAudioSourceContentAnalysis(…, true) は isAudioSourceContentAnalysisIncomplete が false になるまで待つ
        dc->requestAudioSourceContentAnalysis (audioSource, factory->analyzeableContentTypesCount, factory->analyzeableContentTypes, true);
        const auto incomplete { dc->getDocumentController ()->isAudioSourceContentAnalysisIncomplete (dc->getRef (audioSource), ARA::kARAContentTypeNotes) };
        waitForRegionNotes (plugInEntry.get (), dc, ARA::kARAContentGradeDetected, 1000);
        writeText (outDir + "/notes-n.json", describeDocumentNotes (dc));
        ARA_LOG ("document N analyzed (incomplete afterwards: %s)", incomplete ? "yes" : "no");
    }
    setEnv ("GLISS_TEST_EDIT", testEdit.empty () ? nullptr : testEdit.c_str ());

    // ---- A: 作って、描いて、保存する ----
    MemoryArchive archive { factory->documentArchiveID };
    std::vector<float> renderA;
    {
        std::unique_ptr<TestHost> testHost;
        auto dc { createHostAndBasicDocument (plugInEntry.get (), testHost, "GlissARATest A", false, files) };
        renderA = renderDocument (plugInEntry.get (), dc, voice->getSampleRate ());

        // 編集（GLISS_TEST_EDIT）の後のノート（adjusted）。ホストは解析を頼んでいない（エンジンの裏の準備と試験の編集で解析される）
        waitForRegionNotes (plugInEntry.get (), dc, ARA::kARAContentGradeAdjusted, 60000);
        writeText (outDir + "/notes-a.json", describeDocumentNotes (dc));

        if (! dc->supportsPartialPersistency () || ! dc->storeObjectsToArchive (&archive))
        {
            ARA_LOG ("storing the archive failed");
            return 1;
        }
        ARA_LOG ("document A stored");
    }   // ドキュメントを閉じる（プラグインはエンジンを止める）

    const std::string archiveData = archive;
    {
        std::ofstream out (outDir + "/archive.json", std::ios::binary);
        out << archiveData;
    }

    // ---- B: 別の作業場所で、アーカイブだけから作り直す ----
    setEnv ("VOCAL_ENGINE_WORK_DIR", workB.c_str ());
    setEnv ("GLISS_TEST_EDIT", nullptr);

    std::vector<float> renderB, renderC;
    ModelUpdateCounts countsB, countsD;
    bool notesReadyD { false };
    modelUpdateCounts () = {};
    {
        auto testHost { std::make_unique<TestHost> () };
        auto document { testHost->addDocument ("GlissARATest B", plugInEntry.get ()) };
        auto dc { testHost->getDocumentController (document) };

        dc->beginEditing ();
        auto musicalContext { testHost->addMusicalContext (document, "ARA Test Musical Context", { 1.0f, 0.0f, 0.0f }) };
        auto regionSequence { testHost->addRegionSequence (document, "Track 1", musicalContext, { 0.0f, 1.0f, 0.0f }) };
        auto audioSource { testHost->addAudioSource (document, voice.get (), sourceID) };
        dc->enableAudioSourceSamplesAccess (audioSource, true);
        auto audioModification { testHost->addAudioModification (document, audioSource, "Test audio modification 0", modificationID) };
        testHost->addPlaybackRegion (document, audioModification, ARA::kARAPlaybackTransformationNoChanges,
                                     0.0, audioSource->getDuration (), 0.0, audioSource->getDuration (),
                                     regionSequence, "Test playback region", { 0.0f, 0.0f, 1.0f });
        const auto restored { dc->restoreObjectsFromArchive (&archive) };
        dc->endEditing ();

        if (! restored)
        {
            ARA_LOG ("restoring the archive failed");
            return 1;
        }

        renderB = renderDocument (plugInEntry.get (), dc, voice->getSampleRate ());
        renderC = renderDocument (plugInEntry.get (), dc, otherRate);
        pumpHost (plugInEntry.get (), dc, 3000);
        countsB = modelUpdateCounts ();             // ホストはまだノートを読んでいない

        // アーカイブから戻した編集のノート（別の作業場所で解析し直した後）
        waitForRegionNotes (plugInEntry.get (), dc, ARA::kARAContentGradeAdjusted, 60000);
        writeText (outDir + "/notes-b.json", describeDocumentNotes (dc));
    }

    // ---- D: 同じ作業場所で、アーカイブから開き直すだけ（解析は B の作業場所にある。曲を同じ PC で開き直したとき） ----
    modelUpdateCounts () = {};
    {
        auto testHost { std::make_unique<TestHost> () };
        auto document { testHost->addDocument ("GlissARATest D", plugInEntry.get ()) };
        auto dc { testHost->getDocumentController (document) };

        dc->beginEditing ();
        auto musicalContext { testHost->addMusicalContext (document, "ARA Test Musical Context", { 1.0f, 0.0f, 0.0f }) };
        auto regionSequence { testHost->addRegionSequence (document, "Track 1", musicalContext, { 0.0f, 1.0f, 0.0f }) };
        auto audioSource { testHost->addAudioSource (document, voice.get (), sourceID) };
        dc->enableAudioSourceSamplesAccess (audioSource, true);
        auto audioModification { testHost->addAudioModification (document, audioSource, "Test audio modification 0", modificationID) };
        testHost->addPlaybackRegion (document, audioModification, ARA::kARAPlaybackTransformationNoChanges,
                                     0.0, audioSource->getDuration (), 0.0, audioSource->getDuration (),
                                     regionSequence, "Test playback region", { 0.0f, 0.0f, 1.0f });
        const auto restored { dc->restoreObjectsFromArchive (&archive) };
        dc->endEditing ();

        if (! restored)
        {
            ARA_LOG ("restoring the archive failed (D)");
            return 1;
        }

        renderDocument (plugInEntry.get (), dc, voice->getSampleRate ());
        pumpHost (plugInEntry.get (), dc, 8000);     // 同期（ノート・保存用の写し）が済むまで
        countsD = modelUpdateCounts ();

        // ノートは知らせを待たずに読める（もう来ている）。読んで初めて来るなら、上の数は何も確かめていない
        auto* hc { dc->getDocumentController () };
        const auto r { dc->getRef (audioModification) };
        notesReadyD = hc->isAudioModificationContentAvailable (r, ARA::kARAContentTypeNotes)
                      && hc->getAudioModificationContentGrade (r, ARA::kARAContentTypeNotes) == ARA::kARAContentGradeAdjusted;
    }

    writeText (outDir + "/updates.json", "{\"b\": " + countsB.toJson () + ", \"d\": " + countsD.toJson ()
                                         + ", \"d_notes_ready\": " + (notesReadyD ? "true" : "false") + "}");
    if (countsB.changes () != 0 || countsD.changes () != 0 || ! notesReadyD)
    {
        ARA_LOG ("restoring told the host that the document changed (or D had no notes): B %s, D %s, D notes ready %s",
                 countsB.toJson ().c_str (), countsD.toJson ().c_str (), notesReadyD ? "yes" : "no");
        return 1;
    }

    writeFloats (outDir + "/render-a.f32", renderA);
    writeFloats (outDir + "/render-b.f32", renderB);
    writeFloats (outDir + "/render-c.f32", renderC);

    {
        std::ofstream out (outDir + "/summary.json", std::ios::binary);
        out << "{\"source_id\": \"" << sourceID << "\", \"modification_id\": \"" << modificationID << "\", "
            << "\"source_rate\": " << voice->getSampleRate () << ", \"source_frames\": " << voice->getSampleCount () << ", "
            << "\"rate_c\": " << otherRate << ", \"frames_a\": " << renderA.size () << ", \"frames_b\": " << renderB.size ()
            << ", \"frames_c\": " << renderC.size () << "}\n";
    }

    plugInEntry->uninitializeARA ();
    ARA_LOG ("done");
    return 0;
}
