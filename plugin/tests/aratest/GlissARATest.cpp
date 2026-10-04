// GlissARATest: Gliss の ARA プラグインの通し試験（エンジンにつないだ編集 → 再生 → アーカイブの往復）。
// ARA SDK の TestHost の部品（TestHost・ARADocumentController・CompanionAPIs・TestCases）を使い、main だけをこのファイルに差し替える。
//
//   GlissARATest -vst3 <Gliss.vst3> -out <フォルダ> -workB <作業場所 B> [-rate <描画 C の周波数>]
//   GlissARATest -vst3 <Gliss.vst3> -out <フォルダ> -relay <relay_client.py>   （外部の AI からの編集の通し。下の runRelay）
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
//   ソースの音 → source.f32、ID と長さ → summary.json。比べるのは plugin/tests/verify_ara_engine.py。
//
// notes-*.json は {sources: {id: 中身}, modifications: {id: 中身}, regions: [{song_start, mod_start, …, content: 中身}]}、
// 中身 = {available, grade, notes: [[frequency, pitchNumber, volume, startPosition, attackDuration, noteDuration, signalDuration]]}。
// 出力の .f32 はチャンネル 0 の float32（リトルエンディアン）。終了コード 0 = 最後まで流れた（音の正しさは verify が見る）。

#include "TestCases.h"
#include "TestHost.h"
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

        plugInEntry->idleThreadForDuration (50);
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

/** 外部の AI の代わり（relay_client.py）を起動し、終わるまで待つ（その間もホストのメインスレッドの仕事を回す）。終了コードを返す。 */
int runExternalClient (PlugInEntry* plugInEntry, const std::string& script, const std::string& outDir, int timeoutMs)
{
    const auto* python { std::getenv ("GLISS_ENGINE_PYTHON") };
    const auto* cwd { std::getenv ("GLISS_ENGINE_CWD") };
    if (python == nullptr || cwd == nullptr)
    {
        ARA_LOG ("relay: GLISS_ENGINE_PYTHON and GLISS_ENGINE_CWD are required");
        return -1;
    }

    auto commandLine { L"\"" + widen (python) + L"\" \"" + widen (script) + L"\" \"" + widen (outDir) + L"\"" };
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

        clientCode = runExternalClient (plugInEntry, script, outDir, 300000);
        ARA_LOG ("relay: the external client exited with %i", clientCode);
        if (clientCode != 0)
            return 1;

        // 外部の編集は ara_revs（1 秒ごと）で拾われる: 編集の後のノート（adjusted）になるまで待ってから描く
        if (! waitForRegionNotes (plugInEntry, dc, ARA::kARAContentGradeAdjusted, 60000))
            return 1;
        render1 = renderDocument (plugInEntry, dc, voice.getSampleRate ());
        writeText (outDir + "/notes-r.json", describeDocumentNotes (dc));

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
} // namespace

int main (int argc, const char* argv[])
{
    const std::vector<std::string> args (argv, argv + argc);
    ARA::ARASetExternalAssertReference (assertFunctionReference);

    const auto outDir { argument (args, "-out") };
    const auto workB { argument (args, "-workB") };
    const auto relayScript { argument (args, "-relay") };
    const auto otherRate { std::atof (argument (args, "-rate", "48000").c_str ()) };

    auto plugInEntry { PlugInEntry::parsePlugInEntry (args) };
    if (! plugInEntry || outDir.empty () || (workB.empty () && relayScript.empty ()))
    {
        ARA_LOG ("usage: GlissARATest -vst3 <Gliss.vst3> -out <folder> (-workB <work folder for document B> [-rate <Hz>] | -relay <relay_client.py>)");
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

        // アーカイブから戻した編集のノート（別の作業場所で解析し直した後）
        waitForRegionNotes (plugInEntry.get (), dc, ARA::kARAContentGradeAdjusted, 60000);
        writeText (outDir + "/notes-b.json", describeDocumentNotes (dc));
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
