#include "GlissPlaybackRenderer.h"

#include "Diagnostics.h"
#include "ara/RegionMapping.h"

#include <algorithm>
#include <cstring>

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

/** processBlock の出入りを数える（どの出口でも blocksExited が増えるように）。 */
struct BlockScope
{
    BlockScope (std::atomic<juce::int64>& enteredIn, std::atomic<juce::int64>& exitedIn) : exited (exitedIn) { ++enteredIn; }
    ~BlockScope() { ++exited; }

    std::atomic<juce::int64>& exited;
};
}

GlissPlaybackRenderer::GlissPlaybackRenderer (ARA::PlugIn::DocumentController* documentController, RenderContext& contextIn)
    : ARAPlaybackRenderer (documentController), context (contextIn)
{
}

GlissPlaybackRenderer::~GlissPlaybackRenderer()
{
    if (pair != nullptr)
        pair->playback = nullptr;

    delete table.exchange (nullptr);
}

std::unique_ptr<FormatSourceReader> GlissPlaybackRenderer::makeSourceReader (juce::ARAAudioSource* source) const
{
    auto hostReader = std::make_unique<juce::ARAAudioSourceReader> (source);

    if (readDirectly)
        return std::make_unique<FormatSourceReader> (std::move (hostReader));

    const auto readAhead = juce::jmax (4 * maximumSamplesPerBlock, juce::roundToInt (readAheadSeconds * source->getSampleRate()));
    auto buffering = std::make_unique<juce::BufferingAudioReader> (hostReader.release(), *prefetchThread, readAhead);
    auto* bufferingPtr = buffering.get();
    return std::make_unique<FormatSourceReader> (std::move (buffering), bufferingPtr);
}

GlissPlaybackRenderer::SourceSlot* GlissPlaybackRenderer::slotFor (juce::ARAAudioSource* source)
{
    auto& slot = sources[source];

    if (slot == nullptr)
    {
        slot = std::make_unique<SourceSlot>();
        slot->id = juce::String (source->getPersistentID());
        slot->rate = source->getSampleRate();
        slot->channels = (int) source->getChannelCount();
        slot->length = (juce::int64) source->getSampleCount();
        slot->owned = makeSourceReader (source);
        slot->current.store (slot->owned.get());
    }

    return slot.get();
}

std::unique_ptr<GlissPlaybackRenderer::RegionEntry> GlissPlaybackRenderer::makeEntry (const juce::ARAPlaybackRegion* region)
{
    auto* modification = region->getAudioModification<GlissAudioModification>();
    auto* source = modification != nullptr ? modification->getAudioSource() : nullptr;

    if (source == nullptr)
        return nullptr;

    const auto sourceChannels = (int) source->getChannelCount();
    const auto sourceRate = source->getSampleRate();

    if (sourceChannels <= 0 || sourceRate <= 0.0)
        return nullptr;

    auto entry = std::make_unique<RegionEntry>();
    entry->reader.prepare (sampleRate, maximumSamplesPerBlock, juce::jmin (sourceChannels, RegionReader::maxChannels), sourceRate);
    entry->stretch.prepare (sampleRate, maximumSamplesPerBlock, numChannels, sourceRate);
    entry->slot = slotFor (source);
    entry->pcm = modification->getEditedPcm();
    entry->sourceRate = sourceRate;
    return entry;
}

bool GlissPlaybackRenderer::hasEntry (const juce::ARAPlaybackRegion* region) const
{
    return std::any_of (entries.begin(), entries.end(), [region] (const auto& e) { return e.first == region; });
}

void GlissPlaybackRenderer::publishTable()
{
    auto next = std::make_unique<RegionTable>();

    for (const auto& [region, entry] : entries)
        next->items.emplace_back (region, entry.get());

    const auto* old = table.exchange (next.release());

    if (old != nullptr)
    {
        Retired r;
        r.table.reset (old);
        retire (std::move (r));
    }
}

void GlissPlaybackRenderer::retire (Retired&& item)
{
    // 差し替えた後に呼ぶ。この時点で始まっていたブロックがすべて終われば、古いものを使う者はいない
    // （これより後に始まるブロックは、新しい表・リーダーを読む）。
    item.block = blocksEntered.load();
    retired.push_back (std::move (item));
    collectRetired();
}

void GlissPlaybackRenderer::collectRetired()
{
    const auto exited = blocksExited.load();
    retired.erase (std::remove_if (retired.begin(), retired.end(), [exited] (const Retired& r) { return exited >= r.block; }),
                   retired.end());
}

void GlissPlaybackRenderer::logAggregate (const juce::String& line)
{
    diag::logAlways (line);     // 同じ行が続くとまとめる（Diagnostics の logAlways）
}

void GlissPlaybackRenderer::prepareToPlay (double sampleRateIn,
                                           int maximumSamplesPerBlockIn,
                                           int numChannelsIn,
                                           juce::AudioProcessor::ProcessingPrecision precision,
                                           AlwaysNonRealtime alwaysNonRealtime)
{
    ARAPlaybackRenderer::prepareToPlay (sampleRateIn, maximumSamplesPerBlockIn, numChannelsIn, precision, alwaysNonRealtime);

    if (const auto early = unpreparedBlocks.load(); early > 0)
        logAggregate ("renderer: " + juce::String (early) + " block(s) were rendered before prepare");

    sampleRate = sampleRateIn;
    maximumSamplesPerBlock = maximumSamplesPerBlockIn;
    numChannels = numChannelsIn;

    // 常にリアルタイムでない使い方なら、先読みせず直に読む（待ってよいので）。
    readDirectly = alwaysNonRealtime == AlwaysNonRealtime::yes;

    // 前の準備の表・読み出しを手放す（prepareToPlay はブロック処理と同時に呼ばれない）
    delete table.exchange (nullptr);
    entries.clear();
    retired.clear();
    sources.clear();
    skippedRegions = 0;
    readerRefreshes = 0;
    int resampled = 0;

    for (const auto* region : getPlaybackRegions())
    {
        auto entry = makeEntry (region);

        if (entry == nullptr)
        {
            ++skippedRegions;
            continue;
        }

        if (! juce::exactlyEqual (entry->sourceRate, sampleRate))
            ++resampled;

        entries.emplace_back (region, std::move (entry));
    }

    blocksRendered = 0;
    samplesRendered = 0;
    incompleteReads = 0;
    lockMisses = 0;
    unpreparedBlocks = 0;
    noSourceReads = 0;
    unknownRegionBlocks = 0;
    blocksEntered = 0;
    blocksExited = 0;
    syncWaitMs = 0;
    syncWaitTimeouts = 0;
    traceCount = 0;

    tracing = diag::traceEnabled();
    forcedTimeoutMs = diag::forcedReadTimeoutMs();
    forcedSyncWaitMs = juce::SystemStats::getEnvironmentVariable ("GLISS_ARA_SYNC_WAIT_MS", "-1").getIntValue();
    syncWaitBudgetMs = forcedSyncWaitMs >= 0 ? (double) forcedSyncWaitMs : offlineSyncWaitMs;

    if (tracing)
        trace.assign (traceCapacity, {});

    publishTable();
    prepared = true;
    logAggregate ("renderer: prepare " + juce::String (sampleRate) + " Hz, block " + juce::String (maximumSamplesPerBlock)
                  + ", " + juce::String (numChannels) + " ch, " + juce::String ((int) entries.size()) + " region(s), "
                  + juce::String ((int) sources.size()) + " source(s), " + juce::String (resampled) + " resampled, "
                  + juce::String (skippedRegions) + " skipped, " + (readDirectly ? "direct" : "prefetch"));
}

void GlissPlaybackRenderer::releaseResources()
{
    if (prepared)
        dumpTrace();

    prepared = false;
    delete table.exchange (nullptr);
    entries.clear();
    retired.clear();
    sources.clear();
    trace.clear();
    trace.shrink_to_fit();

    ARAPlaybackRenderer::releaseResources();
}

void GlissPlaybackRenderer::refreshSource (juce::ARAAudioSource* source, const char* reason)
{
    if (! prepared || source == nullptr)
        return;

    const auto existed = sources.find (source) != sources.end();
    syncRegions();                       // 準備のときに読めなかったリージョンも、ここで拾う

    const auto found = sources.find (source);

    if (found == sources.end())
        return;

    auto& slot = *found->second;

    if (! existed)
    {
        // syncRegions が今作った（リーダーも新しい）ので、差し替えない
        logAggregate ("renderer: source reader created id=" + slot.id + " reason=" + juce::String (reason));
        return;
    }

    const auto formatChanged = source->getSampleRate() != slot.rate || (int) source->getChannelCount() != slot.channels;

    // プロパティの更新（名前など）では、長さ・形式が変わっていなければ（ARAAudioSourceReader も無効にならないので）差し替えない
    if (std::strcmp (reason, "properties") == 0 && ! formatChanged && (juce::int64) source->getSampleCount() == slot.length)
        return;

    // 新しいリーダーを作ってから入れ替える。古いリーダーは、オーディオスレッドが使い終わるまで retired に置く
    auto fresh = makeSourceReader (source);
    slot.current.store (fresh.get());
    Retired oldReader;
    oldReader.reader = std::move (slot.owned);
    slot.owned = std::move (fresh);
    slot.rate = source->getSampleRate();
    slot.channels = (int) source->getChannelCount();
    slot.length = (juce::int64) source->getSampleCount();
    retire (std::move (oldReader));
    ++readerRefreshes;

    // 形式（周波数・チャンネル数）が変わったソースのリージョンは、読み出し（変換の状態）も作り直す
    if (formatChanged)
    {
        std::vector<Retired> olds;

        for (auto it = entries.begin(); it != entries.end();)
        {
            auto* modification = it->first->getAudioModification<GlissAudioModification>();

            if (modification == nullptr || modification->getAudioSource() != source)
            {
                ++it;
                continue;
            }

            if (auto replacement = makeEntry (it->first))
            {
                Retired old;
                old.entry = std::move (it->second);
                it->second = std::move (replacement);
                olds.push_back (std::move (old));
                ++it;
            }
            else
            {
                ++skippedRegions;
                Retired old;
                old.entry = std::move (it->second);
                olds.push_back (std::move (old));
                it = entries.erase (it);
            }
        }

        publishTable();                  // 表を差し替えてから、古い読み出しを retire する

        for (auto& old : olds)
            retire (std::move (old));
    }

    logAggregate ("renderer: source reader replaced id=" + slot.id + " reason=" + juce::String (reason)
                  + (formatChanged ? " (format changed)" : ""));
}

void GlissPlaybackRenderer::syncRegions()
{
    if (! prepared)
        return;

    bool changed = false;
    const auto& live = getPlaybackRegions();

    for (const auto* region : live)
    {
        if (hasEntry (region))
            continue;

        if (auto entry = makeEntry (region))
        {
            entries.emplace_back (region, std::move (entry));
            changed = true;
        }
    }

    std::vector<Retired> olds;

    for (auto it = entries.begin(); it != entries.end();)
    {
        if (std::find (live.begin(), live.end(), it->first) == live.end())
        {
            Retired old;
            old.entry = std::move (it->second);
            olds.push_back (std::move (old));
            it = entries.erase (it);
            changed = true;
        }
        else
        {
            ++it;
        }
    }

    if (! changed)
        return;

    publishTable();

    for (auto& old : olds)
        retire (std::move (old));

    skippedRegions = (int) live.size() - (int) entries.size();
    logAggregate ("renderer: regions synced, " + juce::String ((int) entries.size()) + " of " + juce::String ((int) live.size())
                  + " region(s) readable");
}

void GlissPlaybackRenderer::didAddPlaybackRegion (ARA::PlugIn::PlaybackRegion*) noexcept
{
    syncRegions();
}

void GlissPlaybackRenderer::willRemovePlaybackRegion (ARA::PlugIn::PlaybackRegion* removed) noexcept
{
    if (! prepared)
        return;

    const auto* region = static_cast<const juce::ARAPlaybackRegion*> (removed);

    for (auto it = entries.begin(); it != entries.end(); ++it)
    {
        if (it->first != region)
            continue;

        Retired old;
        old.entry = std::move (it->second);
        entries.erase (it);
        publishTable();                  // 表から外してから retire する
        retire (std::move (old));
        return;
    }
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
    const BlockScope scope (blocksEntered, blocksExited);

    // ドキュメントの編集中は触らない。待たずに、その区間は無音にする。
    const auto lock = context.getProcessingLock();

    // 入力（ホストが渡す音）は使わず、リージョンの音で置き換える。
    buffer.clear();

    if (! lock.isLocked())
    {
        ++lockMisses;
        return true;
    }

    const auto* regions = table.load();

    if (! prepared.load() || regions == nullptr)
    {
        ++unpreparedBlocks;       // 準備の前に呼ばれた（数えるだけ。次の prepare / release のログに出る）
        return false;
    }

    ++blocksRendered;

    // ホストが持つリージョンの数と、読み出しを持つ数が合わない（読めないリージョンがある）ブロックを数える
    if (getPlaybackRegions().size() != regions->items.size())
        ++unknownRegionBlocks;

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

    for (const auto& item : regions->items)
    {
        const auto* region = item.first;
        auto& entry = *item.second;
        auto* source = entry.slot->current.load();

        if (source == nullptr)
        {
            ++noSourceReads;
            continue;
        }

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
                                                times.scale(), source, entry.pcm.get(), options);
        }
        else
        {
            // 非伸縮は従来の整数の切り出し・読み出しをそのまま使う。
            slice = regions::sliceBlock (times, timeInSamples, numSamples, sampleRate, entry.sourceRate);
            if (slice.isEmpty())
                continue;
            complete = entry.reader.readBlock (buffer, slice.destStart, slice.numSamples, slice.startInSource,
                                               source, entry.pcm.get(), options);
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
    logAggregate ("renderer: release blocks=" + juce::String (blocksRendered.load()) + " samples=" + juce::String (samplesRendered.load())
                  + " incompleteReads=" + juce::String (incompleteReads.load()) + " lockMisses=" + juce::String (lockMisses.load())
                  + " unknownRegionBlocks=" + juce::String (unknownRegionBlocks.load())
                  + " unpreparedBlocks=" + juce::String (unpreparedBlocks.load())
                  + " noSourceReads=" + juce::String (noSourceReads.load())
                  + " skippedRegions=" + juce::String (skippedRegions) + " readerRefreshes=" + juce::String (readerRefreshes)
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
