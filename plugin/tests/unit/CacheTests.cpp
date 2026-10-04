#include "cache/EditedPcm.h"
#include "cache/RegionReader.h"
#include "cache/SourceReader.h"

#include "AllocationCounter.h"

#include <juce_audio_basics/juce_audio_basics.h>
#include <juce_core/juce_core.h>

#include <atomic>
#include <cmath>
#include <cstdio>
#include <limits>
#include <memory>
#include <thread>
#include <vector>

namespace gliss
{

#define CHECK_EXPECT(cond) \
    do { \
        if (!(cond)) { \
            std::printf ("FAIL at line %d: %s\n", __LINE__, #cond); \
            expect (false); \
        } else { \
            expect (true); \
        } \
    } while (0)

#define CHECK_EQUALS(a, b) \
    do { \
        if ((a) != (b)) { \
            std::printf ("FAIL at line %d: %s == %s (got %lld vs %lld)\n", __LINE__, #a, #b, (long long)(a), (long long)(b)); \
            expectEquals ((a), (b)); \
        } else { \
            expectEquals ((a), (b)); \
        } \
    } while (0)

#define CHECK_WITHIN(a, b, tol) \
    do { \
        if (std::abs((a) - (b)) > (tol)) { \
            std::printf ("FAIL at line %d: %f vs %f (diff %f > %f)\n", __LINE__, (double)(a), (double)(b), std::abs((double)(a) - (double)(b)), (double)(tol)); \
            expectWithinAbsoluteError ((a), (b), (tol)); \
        } else { \
            expectWithinAbsoluteError ((a), (b), (tol)); \
        } \
    } while (0)

class CacheTests final : public juce::UnitTest
{
public:
    CacheTests() : juce::UnitTest ("C2 Cache and Playback Reading", "Gliss") {}

    void runTest() override
    {
        beginTest ("1. .f32 and restore/windows dirty update matches full batch rebuild");
        std::printf ("--- Running Test 1 ---\n");
        testDirtyUpdateMatchesBatch();

        beginTest ("2. Thread-safe snapshot replacement during active reading");
        std::printf ("--- Running Test 2 ---\n");
        testThreadSafeReplacement();

        beginTest ("3. Resampling 44.1 kHz to 48 kHz has no discontinuity at window boundaries");
        std::printf ("--- Running Test 3 ---\n");
        testResamplingContinuityAtBoundaries();

        beginTest ("4. Channel conversion (mono to stereo, stereo to mono, and compare mode)");
        std::printf ("--- Running Test 4 ---\n");
        testChannelConversionAndCompareMode();

        beginTest ("5. Resampled output is time-aligned right after a seek and does not depend on block splitting");
        std::printf ("--- Running Test 5 ---\n");
        testResamplingAlignmentAndChunking();

        beginTest ("6. readBlock after prepare does not allocate or free on the audio thread");
        std::printf ("--- Running Test 6 ---\n");
        testNoAllocationOnAudioThread();

        beginTest ("7. Replaced snapshots are released off the audio thread");
        std::printf ("--- Running Test 7 ---\n");
        testOldSnapshotsAreNotReleasedOnTheAudioThread();

        beginTest ("8. Random restore/windows updates match a per-frame model");
        std::printf ("--- Running Test 8 ---\n");
        testIncrementalUpdatesMatchAFrameModel();
    }

private:
    //==========================================================================
    // 1. 差分更新が一括構築と完全一致することを検証
    //==========================================================================
    void testDirtyUpdateMatchesBatch()
    {
        constexpr double sampleRate = 48000.0;
        constexpr int numChannels = 2;

        EditedPcm pcmIncremental (sampleRate, numChannels);

        // テスト用の float データを生成するヘルパー
        auto generateData = [] (int startFrame, int frames, int chans, float baseVal)
        {
            std::vector<float> data ((size_t) frames * (size_t) chans);
            for (int i = 0; i < frames; ++i)
            {
                for (int ch = 0; ch < chans; ++ch)
                {
                    data[(size_t) i * (size_t) chans + (size_t) ch] =
                        baseVal + (float) (startFrame + i) * 0.001f + (float) ch * 0.5f;
                }
            }
            return data;
        };

        // ステップ 1: 初期窓 [1000, 2000) と [3000, 4000) を追加
        auto data1 = generateData (1000, 1000, numChannels, 1.0f);
        auto data2 = generateData (3000, 1000, numChannels, 2.0f);

        std::vector<float> f32Step1;
        f32Step1.insert (f32Step1.end(), data1.begin(), data1.end());
        const auto offset2 = f32Step1.size() * sizeof (float);
        f32Step1.insert (f32Step1.end(), data2.begin(), data2.end());

        std::vector<WindowMeta> metaStep1 = {
            { 1000, 1000, 0 },
            { 3000, 1000, (juce::int64) offset2 }
        };

        CHECK_EXPECT (pcmIncremental.applyDirty ("rev1", true, {}, metaStep1, f32Step1.data(), f32Step1.size()));

        // ステップ 2:
        // - restore [1800, 2500) -> 窓 1 は [1000, 1800) に末尾削り
        // - restore [3400, 3600) -> 窓 2 は [3000, 3400) と [3600, 4000) に2分割
        std::vector<juce::Range<juce::int64>> restoreStep2 = {
            { 1800, 2500 },
            { 3400, 3600 }
        };
        CHECK_EXPECT (pcmIncremental.applyDirty ("rev2", false, restoreStep2, {}, nullptr, 0));

        // ステップ 3:
        // - 新しい窓 [1500, 2200) を追加 -> [1000, 1800) の後半と重なるので、[1000, 1500) に削られ、[1500, 2200) が入る
        // - restore [3550, 4100) -> [3600, 4000) が完全削除される
        // - 新しい窓 [5000, 6000) を追加
        auto data3 = generateData (1500, 700, numChannels, 3.0f);
        auto data4 = generateData (5000, 1000, numChannels, 4.0f);

        std::vector<float> f32Step3;
        f32Step3.insert (f32Step3.end(), data3.begin(), data3.end());
        const auto offset4 = f32Step3.size() * sizeof (float);
        f32Step3.insert (f32Step3.end(), data4.begin(), data4.end());

        std::vector<WindowMeta> metaStep3 = {
            { 1500, 700, 0 },
            { 5000, 1000, (juce::int64) offset4 }
        };
        std::vector<juce::Range<juce::int64>> restoreStep3 = {
            { 3550, 4100 }
        };

        CHECK_EXPECT (pcmIncremental.applyDirty ("rev3", false, restoreStep3, metaStep3, f32Step3.data(), f32Step3.size()));

        // ここで最終状態の窓リストを取得
        auto snapshotInc = pcmIncremental.getSnapshot();
        CHECK_EXPECT (snapshotInc != nullptr);
        const auto& windowsInc = snapshotInc->getWindows();

        // 期待される窓の数:
        // 1. [1000, 1500) (元 data1 の先頭 500 サンプル)
        // 2. [1500, 2200) (data3)
        // 3. [3000, 3400) (元 data2 の先頭 400 サンプル)
        // 4. [5000, 6000) (data4)
        CHECK_EQUALS ((int) windowsInc.size(), 4);

        //----------------------------------------------------------------------
        // 一括構築（batch rebuild）でまったく同じ最終窓を reset=true で作成
        //----------------------------------------------------------------------
        EditedPcm pcmBatch (sampleRate, numChannels);

        // 各窓の生データを準備
        // 窓 1: data1 の 0..500
        std::vector<float> batchData1 ((size_t) 500 * (size_t) numChannels);
        std::copy (data1.begin(), data1.begin() + 500 * numChannels, batchData1.begin());

        // 窓 2: data3 (700 サンプル)
        const auto& batchData2 = data3;

        // 窓 3: data2 の 0..400
        std::vector<float> batchData3 ((size_t) 400 * (size_t) numChannels);
        std::copy (data2.begin(), data2.begin() + 400 * numChannels, batchData3.begin());

        // 窓 4: data4 (1000 サンプル)
        const auto& batchData4 = data4;

        std::vector<float> f32Batch;
        f32Batch.insert (f32Batch.end(), batchData1.begin(), batchData1.end());
        const auto bOff2 = f32Batch.size() * sizeof (float);
        f32Batch.insert (f32Batch.end(), batchData2.begin(), batchData2.end());
        const auto bOff3 = f32Batch.size() * sizeof (float);
        f32Batch.insert (f32Batch.end(), batchData3.begin(), batchData3.end());
        const auto bOff4 = f32Batch.size() * sizeof (float);
        f32Batch.insert (f32Batch.end(), batchData4.begin(), batchData4.end());

        std::vector<WindowMeta> metaBatch = {
            { 1000, 500, 0 },
            { 1500, 700, (juce::int64) bOff2 },
            { 3000, 400, (juce::int64) bOff3 },
            { 5000, 1000, (juce::int64) bOff4 }
        };

        CHECK_EXPECT (pcmBatch.applyDirty ("revBatch", true, {}, metaBatch, f32Batch.data(), f32Batch.size()));

        auto snapshotBatch = pcmBatch.getSnapshot();
        CHECK_EXPECT (snapshotBatch != nullptr);
        const auto& windowsBatch = snapshotBatch->getWindows();

        CHECK_EQUALS ((int) windowsBatch.size(), (int) windowsInc.size());

        for (size_t i = 0; i < windowsInc.size(); ++i)
        {
            const auto& wInc = windowsInc[i];
            const auto& wBatch = windowsBatch[i];

            CHECK_EQUALS (wInc.startFrame, wBatch.startFrame);
            CHECK_EQUALS (wInc.getNumSamples(), wBatch.getNumSamples());
            CHECK_EQUALS (wInc.getNumChannels(), wBatch.getNumChannels());

            for (int ch = 0; ch < wInc.getNumChannels(); ++ch)
            {
                const auto* pInc = wInc.getReadPointer (ch);
                const auto* pBatch = wBatch.getReadPointer (ch);

                for (int s = 0; s < wInc.getNumSamples(); ++s)
                {
                    CHECK_WITHIN (pInc[s], pBatch[s], 1e-6f);
                }
            }
        }
        std::printf ("Test 1 finished.\n");
    }

    //==========================================================================
    // 2. スレッドセーフな差し替えの検証
    //==========================================================================
    void testThreadSafeReplacement()
    {
        constexpr double sampleRate = 48000.0;
        constexpr int numChannels = 2;

        EditedPcm pcm (sampleRate, numChannels);
        std::atomic<bool> stopFlag { false };
        std::atomic<int> readSuccessCount { 0 };
        std::atomic<int> consistencyErrorCount { 0 };

        // ライタースレッド: ひたすら applyDirty でスナップショットを差し替える
        std::thread writer ([&]
        {
            std::vector<float> sampleFloats (2000 * numChannels, 0.42f);
            for (int i = 0; i < 500 && ! stopFlag.load(); ++i)
            {
                const auto start = (juce::int64) ((i % 10) * 100);
                std::vector<WindowMeta> metas = {
                    { start, 500, 0 }
                };
                pcm.applyDirty ("rev_" + juce::String (i), (i % 2 == 0), {}, metas, sampleFloats.data(), sampleFloats.size());
                juce::Thread::sleep (1);
            }
            stopFlag = true;
        });

        // リーダースレッド: tryGetSnapshot() を高頻度で呼び出してデータの整合性を検証
        std::thread reader ([&]
        {
            juce::AudioBuffer<float> dest (numChannels, 256);
            RegionReader regionReader;
            regionReader.prepare (sampleRate, 256, numChannels, sampleRate);

            while (! stopFlag.load())
            {
                auto snap = pcm.tryGetSnapshot();
                if (snap != nullptr)
                {
                    readSuccessCount.fetch_add (1);
                    const auto& windows = snap->getWindows();

                    // 整合性チェック: 昇順かつ重なりなし
                    juce::int64 prevEnd = -1;
                    for (const auto& w : windows)
                    {
                        if (w.startFrame < prevEnd)
                        {
                            consistencyErrorCount.fetch_add (1);
                        }
                        if (w.getNumChannels() != numChannels || w.getNumSamples() <= 0)
                        {
                            consistencyErrorCount.fetch_add (1);
                        }
                        prevEnd = w.getEndFrame();
                    }
                }

                // RegionReader::readBlock も呼び出してみる
                regionReader.readBlock (dest, 0, 256, 100, nullptr, &pcm);
            }
        });

        writer.join();
        reader.join();

        CHECK_EXPECT (readSuccessCount.load() > 0);
        CHECK_EQUALS (consistencyErrorCount.load(), 0);
        std::printf ("Test 2 finished.\n");
    }

    //==========================================================================
    // 3. 44.1 -> 48 kHz 変換で窓の継ぎ目に段差が無いことの検証
    //==========================================================================
    void testResamplingContinuityAtBoundaries()
    {
        constexpr double srcRate = 44100.0;
        constexpr double hostRate = 48000.0;
        constexpr int numChannels = 1;

        // 2秒間の 440 Hz 正弦波（44.1 kHz）を作成
        constexpr int totalSrcSamples = 44100 * 2;
        juce::AudioBuffer<float> fullSrcBuffer (numChannels, totalSrcSamples);
        auto* srcData = fullSrcBuffer.getWritePointer (0);

        constexpr double freq = 440.0;
        for (int i = 0; i < totalSrcSamples; ++i)
        {
            srcData[i] = (float) std::sin (2.0 * juce::MathConstants<double>::pi * freq * (double) i / srcRate);
        }

        BufferSourceReader sourceReader (fullSrcBuffer, srcRate);

        // 窓: サンプル 22050 〜 44100 (0.5s 〜 1.0s) の区間に全く同じ正弦波データを設定
        constexpr int winStart = 22050;
        constexpr int winLen = 22050;
        std::vector<float> winData ((size_t) winLen);
        for (int i = 0; i < winLen; ++i)
            winData[(size_t) i] = srcData[winStart + i];

        EditedPcm pcm (srcRate, numChannels);
        std::vector<WindowMeta> metas = { { winStart, winLen, 0 } };
        CHECK_EXPECT (pcm.applyDirty ("rev1", true, {}, metas, winData.data(), winData.size()));

        // RegionReader を使って 48 kHz 出力で連続ブロック読み出し
        constexpr int blockSize = 256;
        constexpr int totalHostBlocks = 250; // 約 1.33 秒分
        constexpr int totalHostSamples = blockSize * totalHostBlocks;

        juce::AudioBuffer<float> outputBuffer (numChannels, totalHostSamples);
        RegionReader regionReader;
        regionReader.prepare (hostRate, blockSize, numChannels, srcRate);

        const double speedRatio = srcRate / hostRate;
        for (int b = 0; b < totalHostBlocks; ++b)
        {
            const auto hostSample = (juce::int64) (b * blockSize);
            const auto srcSample = (juce::int64) std::llround ((double) hostSample * speedRatio);

            regionReader.readBlock (outputBuffer, b * blockSize, blockSize,
                                    srcSample, &sourceReader, &pcm);
        }

        // 窓の境界に対応するホストサンプル位置:
        // winStart (22050) -> 22050 * (48000 / 44100) = 24000 サンプル (0.5秒)
        // winEnd (44100) -> 44100 * (48000 / 44100) = 48000 サンプル (1.0秒)
        const int boundary1 = 24000;
        const int boundary2 = 48000;

        const auto* out = outputBuffer.getReadPointer (0);

        // 1 階差分と 2 階差分を検査
        // 440 Hz 正弦波（48 kHz、振幅 1.0）の 1 サンプルあたりの変化幅の理論最大値:
        // 2 * pi * 440 / 48000 ≈ 0.0576
        // 段差（不連続）があれば 1 階差分や 2 階差分に大きなスパイクが出る。
        constexpr float maxExpectedFirstDiff = 0.07f;
        constexpr float maxExpectedSecondDiff = 0.01f;

        // 全体の差分を走査（最初の 120 サンプルは、ソースの頭（0 より前は無音）で変換器を満たした過渡応答なので除外）
        for (int i = 120; i < totalHostSamples; ++i)
        {
            const float diff1 = std::abs (out[i] - out[i - 1]);
            const float diff2 = std::abs ((out[i] - out[i - 1]) - (out[i - 1] - out[i - 2]));

            CHECK_EXPECT (diff1 < maxExpectedFirstDiff);
            CHECK_EXPECT (diff2 < maxExpectedSecondDiff);
        }

        // 境界付近（±10 サンプル）での局所的な段差を特に厳密に検査
        for (int bIdx : { boundary1, boundary2 })
        {
            for (int offset = -10; offset <= 10; ++offset)
            {
                const int idx = bIdx + offset;
                if (idx >= 2 && idx < totalHostSamples)
                {
                    const float diff1 = std::abs (out[idx] - out[idx - 1]);
                    const float diff2 = std::abs ((out[idx] - out[idx - 1]) - (out[idx - 1] - out[idx - 2]));

                    CHECK_EXPECT (diff1 < maxExpectedFirstDiff);
                    CHECK_EXPECT (diff2 < maxExpectedSecondDiff);
                }
            }
        }
        std::printf ("Test 3 finished.\n");
    }

    //==========================================================================
    // 4. チャンネル変換と比較モードの検証
    //==========================================================================
    void testChannelConversionAndCompareMode()
    {
        constexpr double sampleRate = 48000.0;

        // A. モノラル -> ステレオ
        {
            juce::AudioBuffer<float> monoSource (1, 100);
            for (int i = 0; i < 100; ++i)
                monoSource.setSample (0, i, (float) (i + 1) * 0.01f);

            BufferSourceReader reader (monoSource, sampleRate);

            juce::AudioBuffer<float> stereoDest (2, 100);
            stereoDest.clear();

            RegionReader regionReader;
            regionReader.prepare (sampleRate, 100, 1, sampleRate);
            regionReader.readBlock (stereoDest, 0, 100, 0, &reader, nullptr);

            const auto* l = stereoDest.getReadPointer (0);
            const auto* r = stereoDest.getReadPointer (1);

            for (int i = 0; i < 100; ++i)
            {
                CHECK_EQUALS (l[i], (float) (i + 1) * 0.01f);
                CHECK_EQUALS (r[i], (float) (i + 1) * 0.01f);
            }
        }

        // B. ステレオ -> モノラル（平均）
        {
            juce::AudioBuffer<float> stereoSource (2, 100);
            for (int i = 0; i < 100; ++i)
            {
                stereoSource.setSample (0, i, 0.4f);
                stereoSource.setSample (1, i, 0.8f);
            }

            BufferSourceReader reader (stereoSource, sampleRate);

            juce::AudioBuffer<float> monoDest (1, 100);
            monoDest.clear();

            RegionReader regionReader;
            regionReader.prepare (sampleRate, 100, 2, sampleRate);
            regionReader.readBlock (monoDest, 0, 100, 0, &reader, nullptr);

            const auto* m = monoDest.getReadPointer (0);
            for (int i = 0; i < 100; ++i)
            {
                CHECK_WITHIN (m[i], 0.6f, 1e-6f);
            }
        }

        // C. 比較モード（compareMode = true）
        {
            juce::AudioBuffer<float> sourceBuf (1, 100);
            sourceBuf.clear(); // 原音はすべて 0.0f
            BufferSourceReader reader (sourceBuf, sampleRate);

            EditedPcm pcm (sampleRate, 1);
            std::vector<float> winData (100, 0.99f);
            std::vector<WindowMeta> metas = { { 0, 100, 0 } };
            pcm.applyDirty ("rev1", true, {}, metas, winData.data(), winData.size());

            juce::AudioBuffer<float> destNormal (1, 100);
            juce::AudioBuffer<float> destCompare (1, 100);
            RegionReader regionReader;
            regionReader.prepare (sampleRate, 100, 1, sampleRate);

            // 通常時: 窓が適用されて 0.99f
            regionReader.readBlock (destNormal, 0, 100, 0, &reader, &pcm, { false });
            CHECK_WITHIN (destNormal.getSample (0, 50), 0.99f, 1e-6f);

            // 比較モード: 窓が無視されて原音（0.0f）
            regionReader.readBlock (destCompare, 0, 100, 0, &reader, &pcm, { true });
            CHECK_WITHIN (destCompare.getSample (0, 50), 0.0f, 1e-6f);
        }

        // D. モノラルの窓をステレオのソースに当てると両チャンネルへ
        {
            juce::AudioBuffer<float> stereoSource (2, 100);
            for (int i = 0; i < 100; ++i)
            {
                stereoSource.setSample (0, i, 0.1f);
                stereoSource.setSample (1, i, 0.2f);
            }
            BufferSourceReader reader (stereoSource, sampleRate);

            EditedPcm pcm (sampleRate, 1);
            std::vector<float> winData (10, 0.9f);
            std::vector<WindowMeta> metas = { { 10, 10, 0 } };
            CHECK_EXPECT (pcm.applyDirty ("rev1", true, {}, metas, winData.data(), winData.size()));

            juce::AudioBuffer<float> dest (2, 100);
            RegionReader regionReader;
            regionReader.prepare (sampleRate, 100, 2, sampleRate);
            regionReader.readBlock (dest, 0, 100, 0, &reader, &pcm);

            CHECK_WITHIN (dest.getSample (0, 15), 0.9f, 1e-6f);
            CHECK_WITHIN (dest.getSample (1, 15), 0.9f, 1e-6f);
            CHECK_WITHIN (dest.getSample (0, 25), 0.1f, 1e-6f);
            CHECK_WITHIN (dest.getSample (1, 25), 0.2f, 1e-6f);

            // E. ソースに無い出力のチャンネルは無音（上書きのとき）
            juce::AudioBuffer<float> quad (4, 100);
            for (int ch = 0; ch < 4; ++ch)
                juce::FloatVectorOperations::fill (quad.getWritePointer (ch), 7.0f, 100);
            regionReader.readBlock (quad, 0, 100, 0, &reader, nullptr);
            CHECK_WITHIN (quad.getSample (0, 50), 0.1f, 1e-6f);
            CHECK_WITHIN (quad.getSample (1, 50), 0.2f, 1e-6f);
            CHECK_WITHIN (quad.getSample (2, 50), 0.0f, 1e-6f);
            CHECK_WITHIN (quad.getSample (3, 50), 0.0f, 1e-6f);

            // F. 周波数の違うスナップショット（設定の食い違い）は当てない
            EditedPcm otherRate (44100.0, 2);
            std::vector<float> otherData (200, 0.9f);
            std::vector<WindowMeta> otherMetas = { { 0, 100, 0 } };
            CHECK_EXPECT (otherRate.applyDirty ("rev1", true, {}, otherMetas, otherData.data(), otherData.size()));
            regionReader.readBlock (dest, 0, 100, 0, &reader, &otherRate);
            CHECK_WITHIN (dest.getSample (0, 50), 0.1f, 1e-6f);
            CHECK_WITHIN (dest.getSample (1, 50), 0.2f, 1e-6f);
        }
        std::printf ("Test 4 finished.\n");
    }

    //==========================================================================
    // 5. 周波数変換: シークの直後から時刻が合い、ブロックの分け方で結果が変わらない
    //==========================================================================
    void testResamplingAlignmentAndChunking()
    {
        constexpr double srcRate = 44100.0;
        constexpr double hostRate = 48000.0;
        constexpr double ratio = srcRate / hostRate;
        constexpr int numChannels = 2;
        constexpr int totalSrc = 44100 * 3;
        constexpr double freq = 440.0;
        const double twoPi = 2.0 * juce::MathConstants<double>::pi;

        auto expected = [&] (double sourcePosition, int ch)
        {
            return (float) std::sin (twoPi * freq * sourcePosition / srcRate + 0.5 * ch);
        };

        juce::AudioBuffer<float> src (numChannels, totalSrc);
        for (int ch = 0; ch < numChannels; ++ch)
            for (int i = 0; i < totalSrc; ++i)
                src.setSample (ch, i, expected ((double) i, ch));
        BufferSourceReader sourceReader (src, srcRate);

        // A. シーク（最初・前へ・後ろへ）の直後の 1 サンプル目から時刻が合う。上限（128）より長いブロックは中で分ける
        {
            RegionReader reader;
            reader.prepare (hostRate, 128, numChannels, srcRate);
            juce::AudioBuffer<float> out (numChannels, 1024);
            float maxError = 0.0f;

            auto run = [&] (juce::int64 startHost, std::initializer_list<int> blockSizes)
            {
                const auto firstStartInSource = (juce::int64) std::llround ((double) startHost * ratio);
                juce::int64 host = startHost;
                for (const auto n : blockSizes)
                {
                    const auto startInSource = (juce::int64) std::llround ((double) host * ratio);
                    out.clear();
                    CHECK_EXPECT (reader.readBlock (out, 0, n, startInSource, &sourceReader, nullptr));
                    for (int ch = 0; ch < numChannels; ++ch)
                        for (int k = 0; k < n; ++k)
                        {
                            const auto position = (double) firstStartInSource + (double) (host - startHost + k) * ratio;
                            maxError = std::max (maxError, std::abs (out.getSample (ch, k) - expected (position, ch)));
                        }
                    host += n;
                }
            };

            run (4800, { 100, 128, 300, 37, 512, 1 });
            run (60000, { 64, 200 });
            run (9600, { 256, 256 });
            std::printf ("Test 5A max error vs ideal: %g\n", (double) maxError);
            // 変換器の遅れが残っていれば誤差は 1 近くになる。0.01 は JUCE の WindowedSinc の利得（約 0.99）の分
            CHECK_EXPECT (maxError < 1.5e-2f);
        }

        // B. 上限 128 で 512 ずつ読む（中で分ける）のと、上限 512 で 64 ずつ読むのが同じ
        {
            constexpr int total = 512 * 40;
            juce::AudioBuffer<float> outA (numChannels, total);
            juce::AudioBuffer<float> outB (numChannels, total);

            RegionReader readerA;
            readerA.prepare (hostRate, 128, numChannels, srcRate);
            for (int host = 0; host < total; host += 512)
                readerA.readBlock (outA, host, 512, (juce::int64) std::llround ((double) host * ratio), &sourceReader, nullptr);

            RegionReader readerB;
            readerB.prepare (hostRate, 512, numChannels, srcRate);
            for (int host = 0; host < total; host += 64)
                readerB.readBlock (outB, host, 64, (juce::int64) std::llround ((double) host * ratio), &sourceReader, nullptr);

            float maxDiff = 0.0f;
            for (int ch = 0; ch < numChannels; ++ch)
                for (int i = 0; i < total; ++i)
                    maxDiff = std::max (maxDiff, std::abs (outA.getSample (ch, i) - outB.getSample (ch, i)));
            CHECK_WITHIN (maxDiff, 0.0f, 1.0e-7f);
        }

        // C. ソースの末尾を越えて読むと無音になり、読めなかった扱いにしない
        {
            RegionReader reader;
            reader.prepare (hostRate, 256, numChannels, srcRate);
            const auto endHost = (juce::int64) std::llround ((double) totalSrc / ratio);
            juce::AudioBuffer<float> out (numChannels, 256);
            bool allComplete = true;
            float maxAfterEnd = 0.0f;
            for (juce::int64 host = endHost - 1024; host < endHost + 1024; host += 256)
            {
                allComplete = reader.readBlock (out, 0, 256, (juce::int64) std::llround ((double) host * ratio), &sourceReader, nullptr) && allComplete;
                for (int k = 0; k < 256; ++k)
                    if (host + k > endHost + 256) // ソースの 200 サンプル（変換器の窓の半分＋余裕）より後
                        for (int ch = 0; ch < numChannels; ++ch)
                            maxAfterEnd = std::max (maxAfterEnd, std::abs (out.getSample (ch, k)));
            }
            CHECK_EXPECT (allComplete);
            CHECK_WITHIN (maxAfterEnd, 0.0f, 0.0f);
        }
        std::printf ("Test 5 finished.\n");
    }

    //==========================================================================
    // 6. prepare の後の readBlock がオーディオスレッドで確保・解放をしない
    //==========================================================================
    void testNoAllocationOnAudioThread()
    {
        // 数え上げが効いていること
        {
            test::ScopedAllocationCounter counter;
            void* volatile p = ::operator new (16);
            ::operator delete (p);
            CHECK_EQUALS (counter.getAllocations(), 1LL);
            CHECK_EQUALS (counter.getDeallocations(), 1LL);
        }

        constexpr double srcRate = 44100.0;
        constexpr double hostRate = 48000.0;
        constexpr int numChannels = 3;
        constexpr int length = 44100 * 2;

        juce::AudioBuffer<float> src (numChannels, length);
        for (int ch = 0; ch < numChannels; ++ch)
            for (int i = 0; i < length; ++i)
                src.setSample (ch, i, (float) std::sin (0.01 * i + ch));

        std::vector<float> winData (2000 * numChannels, 0.25f);
        std::vector<WindowMeta> metas = { { 1000, 2000, 0 }, { 5000, 2000, 0 }, { 9000, 2000, 0 }, { 30000, 2000, 0 } };

        BufferSourceReader resampledSource (src, srcRate);
        EditedPcm resampledPcm (srcRate, numChannels);
        CHECK_EXPECT (resampledPcm.applyDirty ("rev1", true, {}, metas, winData.data(), winData.size()));

        BufferSourceReader sameRateSource (src, hostRate);
        EditedPcm sameRatePcm (hostRate, numChannels);
        CHECK_EXPECT (sameRatePcm.applyDirty ("rev1", true, {}, metas, winData.data(), winData.size()));

        RegionReader resampled;
        resampled.prepare (hostRate, 512, numChannels, srcRate);
        RegionReader sameRate;
        sameRate.prepare (hostRate, 512, numChannels, hostRate);

        juce::AudioBuffer<float> dest (2, 8192);
        RegionReadOptions addOptions;
        addOptions.addToDestBuffer = true;
        RegionReadOptions compareOptions;
        compareOptions.compareMode = true;

        long long allocations = 0, deallocations = 0;
        {
            test::ScopedAllocationCounter counter;

            // 素通し: 上限より長いブロック・3 チャンネル（前はヒープの作業用バッファ）・窓に掛かるブロック（前は窓の列の vector）
            sameRate.readBlock (dest, 0, 8192, 0, &sameRateSource, &sameRatePcm);
            sameRate.readBlock (dest, 0, 256, 900, &sameRateSource, &sameRatePcm, addOptions);
            sameRate.readBlock (dest, 0, 256, 900, &sameRateSource, &sameRatePcm, compareOptions);

            // 変換: 最初・続き・シーク・上限より長いブロック（前は変換器の resize とヒープの作業用バッファ）
            resampled.readBlock (dest, 0, 512, 0, &resampledSource, &resampledPcm);
            resampled.readBlock (dest, 0, 512, (juce::int64) std::llround (512.0 * srcRate / hostRate), &resampledSource, &resampledPcm);
            resampled.readBlock (dest, 0, 4000, 4500, &resampledSource, &resampledPcm, addOptions);
            resampled.readBlock (dest, 0, 4000, 29000, &resampledSource, &resampledPcm);

            // スナップショットの写しと破棄
            for (int i = 0; i < 10; ++i)
                auto snapshot = resampledPcm.tryGetSnapshot();

            allocations = counter.getAllocations();
            deallocations = counter.getDeallocations();
        }
        CHECK_EQUALS (allocations, 0LL);
        CHECK_EQUALS (deallocations, 0LL);
        std::printf ("Test 6 finished.\n");
    }

    //==========================================================================
    // 7. 差し替えた古いスナップショットがオーディオスレッドで解放されない
    //==========================================================================
    void testOldSnapshotsAreNotReleasedOnTheAudioThread()
    {
        constexpr double sampleRate = 48000.0;
        EditedPcm pcm (sampleRate, 1);
        std::vector<float> data (48000, 0.5f);
        std::vector<WindowMeta> metas = { { 0, 48000, 0 } };
        CHECK_EXPECT (pcm.applyDirty ("rev1", true, {}, metas, data.data(), data.size()));
        CHECK_EQUALS ((long long) pcm.releaseUnusedSnapshots(), 0LL); // 誰も読んでいない古いもの（最初の空）は差し替えの時に解放済み

        std::weak_ptr<const EditedPcmSnapshot> watched;
        juce::WaitableEvent acquired, replaced, released;
        long long audioAllocations = -1, audioDeallocations = -1;
        bool gotSnapshot = false;

        std::thread audio ([&]
        {
            auto snapshot = pcm.tryGetSnapshot();
            gotSnapshot = snapshot != nullptr;
            watched = snapshot;
            acquired.signal();
            replaced.wait (10000);
            {
                test::ScopedAllocationCounter counter;
                snapshot.reset(); // 最後の参照ならここで窓の PCM ごと解放される
                audioAllocations = counter.getAllocations();
                audioDeallocations = counter.getDeallocations();
            }
            released.signal();
        });

        acquired.wait (10000);
        CHECK_EXPECT (pcm.applyDirty ("rev2", true, {}, metas, data.data(), data.size()));
        CHECK_EQUALS ((long long) pcm.releaseUnusedSnapshots(), 1LL); // オーディオスレッドが持っている間は残す
        replaced.signal();
        released.wait (10000);
        audio.join();

        CHECK_EXPECT (gotSnapshot);
        CHECK_EQUALS (audioAllocations, 0LL);
        CHECK_EQUALS (audioDeallocations, 0LL);
        CHECK_EXPECT (! watched.expired());                            // 手放しても解放待ちに残っている
        CHECK_EQUALS ((long long) pcm.releaseUnusedSnapshots(), 0LL);  // ここ（オーディオスレッド以外）で解放する
        CHECK_EXPECT (watched.expired());
        std::printf ("Test 7 finished.\n");
    }

    //==========================================================================
    // 8. ランダムな restore・windows の差分更新を、1 フレームずつの模型と突き合わせる
    //==========================================================================
    void testIncrementalUpdatesMatchAFrameModel()
    {
        constexpr double sampleRate = 48000.0;
        constexpr int numChannels = 2;
        constexpr int length = 20000;
        juce::Random random (12345);

        juce::AudioBuffer<float> src (numChannels, length);
        for (int ch = 0; ch < numChannels; ++ch)
            for (int i = 0; i < length; ++i)
                src.setSample (ch, i, -1.0f - (float) i * 1.0e-4f - (float) ch * 0.5f);
        BufferSourceReader sourceReader (src, sampleRate);

        // 期待する出力（原音に戻した所は原音、窓を置いた所は窓の値）
        std::vector<float> model ((size_t) length * numChannels);
        auto restoreModel = [&] (int start, int end)
        {
            for (int i = start; i < end; ++i)
                for (int ch = 0; ch < numChannels; ++ch)
                    model[(size_t) i * numChannels + (size_t) ch] = src.getSample (ch, i);
        };
        restoreModel (0, length);

        EditedPcm pcm (sampleRate, numChannels);
        RegionReader reader;
        reader.prepare (sampleRate, 512, numChannels, sampleRate);
        juce::AudioBuffer<float> out (numChannels, length);
        int mismatches = 0, layoutErrors = 0, applyFailures = 0;

        for (int round = 0; round < 200; ++round)
        {
            const bool reset = round % 50 == 0;
            if (reset)
                restoreModel (0, length);

            std::vector<juce::Range<juce::int64>> restore;
            const auto numRestore = reset ? 0 : random.nextInt (4);
            for (int r = 0; r < numRestore; ++r)
            {
                const auto start = random.nextInt (length);
                const auto end = std::min (length, start + 1 + random.nextInt (3000));
                restore.push_back ({ start, end });
                restoreModel (start, end);
            }

            std::vector<WindowMeta> metas;
            std::vector<float> f32;
            const auto numWindows = random.nextInt (4) + (reset ? 3 : 0);
            for (int w = 0; w < numWindows; ++w)
            {
                const auto start = random.nextInt (length);
                const auto frames = std::min (length, start + 1 + random.nextInt (2500)) - start;
                metas.push_back ({ start, frames, (juce::int64) (f32.size() * sizeof (float)) });
                for (int i = 0; i < frames; ++i)
                    for (int ch = 0; ch < numChannels; ++ch)
                    {
                        const auto value = (float) round + (float) (start + i) * 1.0e-4f + (float) ch * 0.25f;
                        f32.push_back (value);
                        model[(size_t) (start + i) * numChannels + (size_t) ch] = value;
                    }
            }

            if (! pcm.applyDirty (juce::String (round), reset, restore, metas, f32.data(), f32.size()))
                ++applyFailures;

            // 窓の列: 開始の順・重ならない・PCM の範囲の中
            const auto snapshot = pcm.getSnapshot();
            juce::int64 previousEnd = std::numeric_limits<juce::int64>::min();
            for (const auto& w : snapshot->getWindows())
            {
                if (w.startFrame < previousEnd || w.numSamples <= 0 || w.offset < 0
                    || w.offset + w.numSamples > w.pcm->getNumSamples())
                    ++layoutErrors;
                previousEnd = w.getEndFrame();
            }

            // ばらばらの長さのブロック（上限 512 を越えるものを含む）で全体を読む
            for (int pos = 0; pos < length;)
            {
                const auto n = std::min (length - pos, 1 + random.nextInt (1500));
                reader.readBlock (out, pos, n, pos, &sourceReader, &pcm);
                pos += n;
            }

            for (int i = 0; i < length; ++i)
                for (int ch = 0; ch < numChannels; ++ch)
                    if (out.getSample (ch, i) != model[(size_t) i * numChannels + (size_t) ch])
                    {
                        if (mismatches == 0)
                            std::printf ("first mismatch: round %d frame %d ch %d (%f vs %f)\n", round, i, ch,
                                         (double) out.getSample (ch, i), (double) model[(size_t) i * numChannels + (size_t) ch]);
                        ++mismatches;
                    }
        }

        CHECK_EQUALS (applyFailures, 0);
        CHECK_EQUALS (layoutErrors, 0);
        CHECK_EQUALS (mismatches, 0);
        CHECK_EQUALS ((long long) pcm.releaseUnusedSnapshots(), 0LL);
        std::printf ("Test 8 finished.\n");
    }
};

static CacheTests cacheTests;

} // namespace gliss
