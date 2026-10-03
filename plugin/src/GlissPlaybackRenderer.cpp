#include "GlissPlaybackRenderer.h"

#include "Diagnostics.h"
#include "ara/RegionMapping.h"

namespace gliss
{

namespace
{
/** ホストが「リアルタイムでない」描画のときに、原音の先読みの完了を待つ上限（ミリ秒）。 */
constexpr int offlineReadTimeoutMs = 500;

/** 非リアルタイムの描画で、同期の完了を待つ上限（prepareToPlay ごと。docs/ara-plugin.md の「再生」）。 */
constexpr double offlineSyncWaitMs = 10000.0;

/** 先読みの長さ（秒）。 */
constexpr double readAheadSeconds = 4.0;

/** 検証用の記録の上限。 */
constexpr size_t traceCapacity = 1 << 17;
}

GlissPlaybackRenderer::GlissPlaybackRenderer (ARA::PlugIn::DocumentController* documentController, RenderContext& contextIn)
    : ARAPlaybackRenderer (documentController), context (contextIn)
{
}

GlissPlaybackRenderer::~GlissPlaybackRenderer() = default;

void GlissPlaybackRenderer::prepareToPlay (double sampleRateIn,
                                           int maximumSamplesPerBlockIn,
                                           int numChannelsIn,
                                           juce::AudioProcessor::ProcessingPrecision precision,
                                           AlwaysNonRealtime alwaysNonRealtime)
{
    ARAPlaybackRenderer::prepareToPlay (sampleRateIn, maximumSamplesPerBlockIn, numChannelsIn, precision, alwaysNonRealtime);

    sampleRate = sampleRateIn;
    maximumSamplesPerBlock = maximumSamplesPerBlockIn;
    numChannels = numChannelsIn;

    // 常にリアルタイムでない使い方なら、先読みせず直に読む（待ってよいので）。
    const auto readDirectly = alwaysNonRealtime == AlwaysNonRealtime::yes;

    regionEntries.clear();
    sourceReaders.clear();
    int resampled = 0;

    for (const auto* region : getPlaybackRegions())
    {
        auto* modification = region->getAudioModification<GlissAudioModification>();
        auto* source = modification->getAudioSource();
        const auto sourceChannels = (int) source->getChannelCount();
        const auto sourceRate = source->getSampleRate();

        if (sourceChannels <= 0 || sourceRate <= 0.0)
            continue;

        auto& sourceReader = sourceReaders[source];

        if (sourceReader == nullptr)
        {
            auto hostReader = std::make_unique<juce::ARAAudioSourceReader> (source);

            if (readDirectly)
            {
                sourceReader = std::make_unique<FormatSourceReader> (std::move (hostReader));
            }
            else
            {
                const auto readAhead = juce::jmax (4 * maximumSamplesPerBlock, juce::roundToInt (readAheadSeconds * sourceRate));
                auto buffering = std::make_unique<juce::BufferingAudioReader> (hostReader.release(), *prefetchThread, readAhead);
                auto* bufferingPtr = buffering.get();
                sourceReader = std::make_unique<FormatSourceReader> (std::move (buffering), bufferingPtr);
            }
        }

        auto entry = std::make_unique<RegionEntry>();
        entry->reader.prepare (sampleRate, maximumSamplesPerBlock, juce::jmin (sourceChannels, RegionReader::maxChannels), sourceRate);
        entry->stretch.prepare (sampleRate, maximumSamplesPerBlock, numChannels, sourceRate);
        entry->source = sourceReader.get();
        entry->pcm = modification->getEditedPcm();
        entry->sourceRate = sourceRate;
        regionEntries.emplace (region, std::move (entry));

        if (! juce::exactlyEqual (sourceRate, sampleRate))
            ++resampled;
    }

    blocksRendered = 0;
    samplesRendered = 0;
    incompleteReads = 0;
    lockMisses = 0;
    syncWaitMs = 0;
    syncWaitTimeouts = 0;
    traceCount = 0;

    tracing = diag::traceEnabled();
    forcedTimeoutMs = diag::forcedReadTimeoutMs();
    forcedSyncWaitMs = juce::SystemStats::getEnvironmentVariable ("GLISS_ARA_SYNC_WAIT_MS", "-1").getIntValue();
    syncWaitBudgetMs = forcedSyncWaitMs >= 0 ? (double) forcedSyncWaitMs : offlineSyncWaitMs;

    if (tracing)
        trace.assign (traceCapacity, {});

    prepared = true;
    diag::log ("renderer: prepare " + juce::String (sampleRate) + " Hz, block " + juce::String (maximumSamplesPerBlock)
               + ", " + juce::String (numChannels) + " ch, " + juce::String ((int) regionEntries.size()) + " region(s), "
               + juce::String ((int) sourceReaders.size()) + " source(s), " + juce::String (resampled) + " resampled, "
               + (readDirectly ? "direct" : "prefetch"));
}

void GlissPlaybackRenderer::releaseResources()
{
    if (prepared)
        dumpTrace();

    regionEntries.clear();
    sourceReaders.clear();
    trace.clear();
    trace.shrink_to_fit();
    prepared = false;

    ARAPlaybackRenderer::releaseResources();
}

void GlissPlaybackRenderer::waitForSync (bool offline) noexcept
{
    // 非リアルタイムの描画（と、検証用に GLISS_ARA_SYNC_WAIT_MS を指定したとき）だけ待つ。オーディオスレッドのリアルタイムの描画では待たない。
    if (! (offline || forcedSyncWaitMs >= 0) || syncWaitBudgetMs <= 0.0 || context.isSyncSettled())
        return;

    const auto start = juce::Time::getMillisecondCounterHiRes();

    while (! context.isSyncSettled())
    {
        if (juce::Time::getMillisecondCounterHiRes() - start >= syncWaitBudgetMs)
            break;

        juce::Thread::sleep (5);
    }

    const auto waited = juce::Time::getMillisecondCounterHiRes() - start;
    syncWaitBudgetMs -= waited;
    syncWaitMs += (juce::int64) waited;

    if (! context.isSyncSettled())
        ++syncWaitTimeouts;
}

bool GlissPlaybackRenderer::processBlock (juce::AudioBuffer<float>& buffer,
                                          juce::AudioProcessor::Realtime realtime,
                                          const juce::AudioPlayHead::PositionInfo& positionInfo) noexcept
{
    // ドキュメントの編集中は触らない。待たずに、その区間は無音にする。
    const auto lock = context.getProcessingLock();

    // 入力（ホストが渡す音）は使わず、リージョンの音で置き換える。
    buffer.clear();

    if (! lock.isLocked())
    {
        ++lockMisses;
        return true;
    }

    if (! prepared)
        return false;

    ++blocksRendered;

    if (! positionInfo.getIsPlaying())
        return true;

    const auto offline = realtime == juce::AudioProcessor::Realtime::no;
    waitForSync (offline);

    const auto numSamples = buffer.getNumSamples();
    const auto timeInSamples = positionInfo.getTimeInSamples().orFallback (0);

    RegionReadOptions options;
    options.compareMode = context.isCompareMode();
    options.addToDestBuffer = true;
    options.timeoutMs = forcedTimeoutMs >= 0 ? forcedTimeoutMs : offline ? offlineReadTimeoutMs : 0;

    for (const auto* region : getPlaybackRegions())
    {
        const auto found = regionEntries.find (region);

        if (found == regionEntries.end())
            continue;

        auto& entry = *found->second;

        // ホストの時間でのリージョンの範囲と、このブロックの重なり。ヘッド・テールは使わない。
        RegionTimes times;
        times.songStart = region->getStartInPlaybackTime();
        times.songEnd = region->getEndInPlaybackTime();
        times.modStart = region->getStartInAudioModificationTime();
        times.modEnd = region->getEndInAudioModificationTime();
        times.normalize (region->isTimestretchEnabled());

        regions::BlockSlice slice;
        bool complete = false;
        if (times.isStretched())
        {
            const auto stretched = regions::sliceStretchedBlock (times, timeInSamples, numSamples, sampleRate);
            if (stretched.isEmpty())
                continue;
            slice.destStart = stretched.destStart;
            slice.numSamples = stretched.numSamples;
            slice.startInSource = regions::samplePosition (stretched.startInModification / sampleRate, entry.sourceRate);
            complete = entry.stretch.readBlock (buffer, slice.destStart, slice.numSamples,
                                                timeInSamples + slice.destStart, stretched.startInModification,
                                                times.scale(), entry.source, entry.pcm.get(), options);
        }
        else
        {
            // 非伸縮は従来の整数の切り出し・読み出しをそのまま使う。
            slice = regions::sliceBlock (times, timeInSamples, numSamples, sampleRate, entry.sourceRate);
            if (slice.isEmpty())
                continue;
            complete = entry.reader.readBlock (buffer, slice.destStart, slice.numSamples, slice.startInSource,
                                               entry.source, entry.pcm.get(), options);
        }

        if (! complete)
            ++incompleteReads;

        samplesRendered += slice.numSamples;

        if (tracing)
        {
            const auto index = traceCount.fetch_add (1);

            if (index < trace.size())
            {
                auto& t = trace[index];
                t.timeInSamples = timeInSamples + slice.destStart;
                t.startInSource = slice.startInSource;
                t.numSamples = slice.numSamples;
                t.complete = complete;
                t.sum = 0.0;
                t.sumSquares = 0.0;

                // リージョンが重ならないとき（検証のホスト）、ここはこのリージョンの音だけ。
                const auto* samples = buffer.getReadPointer (0, slice.destStart);

                for (int i = 0; i < slice.numSamples; ++i)
                {
                    t.sum += samples[i];
                    t.sumSquares += (double) samples[i] * samples[i];
                }
            }
        }
    }

    return true;
}

void GlissPlaybackRenderer::dumpTrace()
{
    diag::log ("renderer: release blocks=" + juce::String (blocksRendered.load()) + " samples=" + juce::String (samplesRendered.load())
               + " incompleteReads=" + juce::String (incompleteReads.load()) + " lockMisses=" + juce::String (lockMisses.load())
               + " syncWaitMs=" + juce::String (syncWaitMs.load()) + " syncWaitTimeouts=" + juce::String (syncWaitTimeouts.load()));

    if (! tracing)
        return;

    const auto count = juce::jmin (traceCount.load(), trace.size());

    for (size_t i = 0; i < count; ++i)
    {
        const auto& t = trace[i];
        diag::log ("trace t=" + juce::String (t.timeInSamples) + " src=" + juce::String (t.startInSource) + " n=" + juce::String (t.numSamples)
                   + " complete=" + juce::String (t.complete ? 1 : 0) + " sum=" + juce::String (t.sum, 9) + " sumsq=" + juce::String (t.sumSquares, 9));
    }
}

} // namespace gliss
