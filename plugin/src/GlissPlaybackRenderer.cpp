#include "GlissPlaybackRenderer.h"

#include "Diagnostics.h"

namespace gliss
{

namespace
{
/** ホストが「リアルタイムでない」描画のときに、先読みの完了を待つ上限（ミリ秒）。 */
constexpr int offlineReadTimeoutMs = 500;

/** 先読みの長さ（秒）。 */
constexpr double readAheadSeconds = 4.0;

/** 検証用の記録の上限。 */
constexpr size_t traceCapacity = 1 << 17;
}

GlissPlaybackRenderer::GlissPlaybackRenderer (ARA::PlugIn::DocumentController* documentController,
                                              ProcessingLockInterface& lockInterfaceIn)
    : ARAPlaybackRenderer (documentController), lockInterface (lockInterfaceIn)
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
    const auto readAhead = juce::jmax (4 * maximumSamplesPerBlock, juce::roundToInt (readAheadSeconds * sampleRate));

    sourceReaders.clear();
    auto maxChannels = numChannels;

    for (const auto* region : getPlaybackRegions())
    {
        auto* source = region->getAudioModification()->getAudioSource();

        if (sourceReaders.find (source) != sourceReaders.end())
            continue;

        SourceReader entry;
        entry.supported = juce::exactlyEqual (source->getSampleRate(), sampleRate) && source->getChannelCount() > 0;

        if (entry.supported)
        {
            maxChannels = juce::jmax (maxChannels, (int) source->getChannelCount());
            auto hostReader = std::make_unique<juce::ARAAudioSourceReader> (source);

            if (readDirectly)
            {
                entry.reader = std::move (hostReader);
            }
            else
            {
                auto buffering = std::make_unique<juce::BufferingAudioReader> (hostReader.release(), *prefetchThread, readAhead);
                entry.buffered = buffering.get();
                entry.reader = std::move (buffering);
            }
        }
        else
        {
            diag::log ("renderer: source '" + juce::String (source->getPersistentID()) + "' is " + juce::String (source->getSampleRate())
                       + " Hz but the host renders at " + juce::String (sampleRate) + " Hz; not played (stage 1)");
        }

        sourceReaders.emplace (source, std::move (entry));
    }

    scratch.setSize (maxChannels, maximumSamplesPerBlock);

    blocksRendered = 0;
    samplesRendered = 0;
    incompleteReads = 0;
    unsupportedRegions = 0;
    lockMisses = 0;
    traceCount = 0;

    tracing = diag::traceEnabled();
    forcedTimeoutMs = diag::forcedReadTimeoutMs();

    if (tracing)
        trace.assign (traceCapacity, {});

    prepared = true;
    diag::log ("renderer: prepare " + juce::String (sampleRate) + " Hz, block " + juce::String (maximumSamplesPerBlock)
               + ", " + juce::String (numChannels) + " ch, " + juce::String ((int) getPlaybackRegions().size()) + " region(s), "
               + juce::String ((int) sourceReaders.size()) + " source(s), " + (readDirectly ? "direct" : "prefetch"));
}

void GlissPlaybackRenderer::releaseResources()
{
    if (prepared)
        dumpTrace();

    sourceReaders.clear();
    scratch.setSize (0, 0);
    trace.clear();
    trace.shrink_to_fit();
    prepared = false;

    ARAPlaybackRenderer::releaseResources();
}

bool GlissPlaybackRenderer::processBlock (juce::AudioBuffer<float>& buffer,
                                          juce::AudioProcessor::Realtime realtime,
                                          const juce::AudioPlayHead::PositionInfo& positionInfo) noexcept
{
    // ドキュメントの編集中は触らない。待たずに、その区間は無音にする。
    const auto lock = lockInterface.getProcessingLock();

    // 入力（ホストが渡す音）は使わず、リージョンの音で置き換える。
    buffer.clear();

    if (! lock.isLocked())
    {
        ++lockMisses;
        return true;
    }

    const auto numSamples = buffer.getNumSamples();

    if (! prepared || numSamples > scratch.getNumSamples())
        return false;

    ++blocksRendered;

    if (! positionInfo.getIsPlaying())
        return true;

    const auto timeInSamples = positionInfo.getTimeInSamples().orFallback (0);
    const auto blockRange = juce::Range<juce::int64>::withStartAndLength (timeInSamples, numSamples);
    const auto timeoutMs = forcedTimeoutMs >= 0 ? forcedTimeoutMs
                         : realtime == juce::AudioProcessor::Realtime::no ? offlineReadTimeoutMs : 0;

    for (const auto* region : getPlaybackRegions())
    {
        // ホストの時間（曲の時間）でのリージョンの範囲と、このブロックの重なり。
        // ヘッド・テールの時間は使わない（時間を伸ばさない）ので includeHeadAndTail は no。
        const auto playbackRange = region->getSampleRange (sampleRate, juce::ARAPlaybackRegion::IncludeHeadAndTail::no);
        auto renderRange = blockRange.getIntersectionWith (playbackRange);

        if (renderRange.isEmpty())
            continue;

        // ソースの時間での範囲。曲の時間との差が、読み出しの位置のずれになる（時間を伸ばすなら、ここで考える）。
        const juce::Range<juce::int64> modificationRange { region->getStartInAudioModificationSamples(),
                                                           region->getEndInAudioModificationSamples() };
        const auto modificationOffset = modificationRange.getStart() - playbackRange.getStart();

        renderRange = renderRange.getIntersectionWith (modificationRange.movedToStartAt (playbackRange.getStart()));

        if (renderRange.isEmpty())
            continue;

        const auto readerIt = sourceReaders.find (region->getAudioModification()->getAudioSource());

        if (readerIt == sourceReaders.end() || ! readerIt->second.supported)
        {
            ++unsupportedRegions;
            continue;
        }

        auto& entry = readerIt->second;

        if (entry.buffered != nullptr)
            entry.buffered->setReadTimeout (timeoutMs);

        const auto sourceChannels = (int) entry.reader->numChannels;
        const auto numToRead = (int) renderRange.getLength();
        const auto startInBuffer = (int) (renderRange.getStart() - blockRange.getStart());
        const auto startInSource = renderRange.getStart() + modificationOffset;

        juce::AudioBuffer<float> readBuffer (scratch.getArrayOfWritePointers(), juce::jmin (sourceChannels, scratch.getNumChannels()), numToRead);
        const auto complete = entry.reader->read (&readBuffer, 0, numToRead, startInSource, true, true);

        if (! complete)
            ++incompleteReads;

        // 先読みが間に合わなかった範囲は readBuffer の中で無音になっている。読めたところはそのまま使う。
        for (int channel = 0; channel < buffer.getNumChannels(); ++channel)
            buffer.addFrom (channel, startInBuffer, readBuffer, juce::jmin (channel, readBuffer.getNumChannels() - 1), 0, numToRead);

        samplesRendered += numToRead;

        if (tracing)
        {
            const auto index = traceCount.fetch_add (1);

            if (index < trace.size())
            {
                auto& t = trace[index];
                t.timeInSamples = timeInSamples + startInBuffer;
                t.startInSource = startInSource;
                t.numSamples = numToRead;
                t.complete = complete;
                t.sum = 0.0;
                t.sumSquares = 0.0;

                const auto* samples = readBuffer.getReadPointer (0);

                for (int i = 0; i < numToRead; ++i)
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
               + " incompleteReads=" + juce::String (incompleteReads.load()) + " unsupportedRegions=" + juce::String (unsupportedRegions.load())
               + " lockMisses=" + juce::String (lockMisses.load()));

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
