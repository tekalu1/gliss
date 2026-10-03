#include "EditedPcm.h"

#include <algorithm>
#include <atomic>
#include <limits>

namespace gliss
{

//==============================================================================
EditedPcmSnapshot::EditedPcmSnapshot (juce::String revIn,
                                      double sampleRateIn,
                                      int numChannelsIn,
                                      std::vector<EditedWindow> windowsIn)
    : rev (std::move (revIn)),
      sampleRate (sampleRateIn),
      numChannels (numChannelsIn),
      windows (std::move (windowsIn))
{
}

size_t EditedPcmSnapshot::findFirstWindowEndingAfter (juce::int64 frame) const noexcept
{
    // 窓は開始の順に並び重ならないので、終わりも昇順
    const auto it = std::partition_point (windows.begin(), windows.end(),
                                          [frame] (const EditedWindow& w) { return w.getEndFrame() <= frame; });
    return (size_t) std::distance (windows.begin(), it);
}

//==============================================================================
EditedPcm::EditedPcm (double sampleRateIn, int numChannelsIn)
    : sampleRate (sampleRateIn),
      numChannels (numChannelsIn),
      currentSnapshot (std::make_shared<EditedPcmSnapshot> (juce::String(), sampleRateIn, numChannelsIn, std::vector<EditedWindow>()))
{
}

std::shared_ptr<const EditedPcmSnapshot> EditedPcm::tryGetSnapshot() const noexcept
{
    const juce::SpinLock::ScopedTryLockType lock (spinLock);
    if (lock.isLocked())
        return currentSnapshot;
    return nullptr;
}

std::shared_ptr<const EditedPcmSnapshot> EditedPcm::getSnapshot() const
{
    const juce::SpinLock::ScopedLockType lock (spinLock);
    return currentSnapshot;
}

juce::String EditedPcm::getRev() const
{
    const auto snapshot = getSnapshot();
    return snapshot != nullptr ? snapshot->getRev() : juce::String();
}

void EditedPcm::publish (std::shared_ptr<const EditedPcmSnapshot> next)
{
    {
        // ロックの中ではポインタを入れ替えるだけ（確保も解放もしない）
        const juce::SpinLock::ScopedLockType lock (spinLock);
        std::swap (currentSnapshot, next);
    }

    // next は差し替える前のもの。オーディオスレッドがまだ読んでいるかもしれないので、ここでは解放しない
    const std::lock_guard<std::mutex> guard (retiredLock);
    if (next != nullptr)
        retired.push_back (std::move (next));
    releaseUnusedSnapshotsLocked();
}

size_t EditedPcm::releaseUnusedSnapshots()
{
    const std::lock_guard<std::mutex> guard (retiredLock);
    return releaseUnusedSnapshotsLocked();
}

size_t EditedPcm::releaseUnusedSnapshotsLocked()
{
    // 解放待ちのものは currentSnapshot ではないので、オーディオスレッドが新たに参照を取ることはない。
    // use_count が 1（この列だけ）になったら、それ以降も 1 のまま。
    const auto unused = std::partition (retired.begin(), retired.end(),
                                        [] (const std::shared_ptr<const EditedPcmSnapshot>& s) { return s.use_count() > 1; });
    if (unused != retired.end())
    {
        // 読み手が参照を手放す前に読んだ内容より後に解放する
        std::atomic_thread_fence (std::memory_order_acquire);
        retired.erase (unused, retired.end());
    }
    return retired.size();
}

void EditedPcm::setFormat (double newSampleRate, int newNumChannels)
{
    sampleRate = newSampleRate;
    numChannels = newNumChannels;
    publish (std::make_shared<EditedPcmSnapshot> (juce::String(), sampleRate, numChannels, std::vector<EditedWindow>()));
}

void EditedPcm::clear()
{
    publish (std::make_shared<EditedPcmSnapshot> (juce::String(), sampleRate, numChannels, std::vector<EditedWindow>()));
}

void EditedPcm::removeOverlappingRanges (std::vector<EditedWindow>& list,
                                         juce::Range<juce::int64> rangeToRemove)
{
    if (rangeToRemove.isEmpty())
        return;

    const auto rStart = rangeToRemove.getStart();
    const auto rEnd = rangeToRemove.getEnd();

    std::vector<EditedWindow> result;
    result.reserve (list.size() + 1);

    // 範囲の外の部分だけを残す（PCM は写さず、offset と numSamples を変える）
    for (auto& w : list)
    {
        const auto wStart = w.startFrame;
        const auto wEnd = w.getEndFrame();

        if (rEnd <= wStart || rStart >= wEnd)
        {
            result.push_back (std::move (w));
            continue;
        }

        if (rStart > wStart)
        {
            auto head = w;
            head.numSamples = (int) (rStart - wStart);
            result.push_back (std::move (head));
        }

        if (rEnd < wEnd)
        {
            auto tail = w;
            tail.offset += (int) (rEnd - wStart);
            tail.startFrame = rEnd;
            tail.numSamples = (int) (wEnd - rEnd);
            result.push_back (std::move (tail));
        }
    }

    list = std::move (result);
}

void EditedPcm::applyRestoreRanges (std::vector<EditedWindow>& list,
                                    const std::vector<juce::Range<juce::int64>>& restore)
{
    for (const auto& r : restore)
        removeOverlappingRanges (list, r);
}

bool EditedPcm::applyDirty (const juce::String& rev,
                            bool reset,
                            const std::vector<juce::Range<juce::int64>>& restore,
                            const std::vector<WindowMeta>& windows,
                            juce::InputStream& f32Stream)
{
    if (numChannels <= 0)
        return false;

    // 窓の列を写す（PCM は共有するので、写すのは窓の数ぶんの小さな構造体だけ）。ロックはポインタを取る間だけ
    std::vector<EditedWindow> workingWindows;
    if (! reset)
        if (const auto base = getSnapshot())
            workingWindows = base->getWindows();

    // 1. restore 範囲を適用
    applyRestoreRanges (workingWindows, restore);

    // 2. windows を読み込んで追加
    std::vector<float> interleaved;
    for (const auto& meta : windows)
    {
        if (meta.frames <= 0)
            continue;

        const auto totalFloats = (size_t) meta.frames * (size_t) numChannels;
        if (meta.byteOffset < 0 || totalFloats * sizeof (float) > (size_t) std::numeric_limits<int>::max())
            return false;

        if (! f32Stream.setPosition (meta.byteOffset))
            return false;

        interleaved.resize (totalFloats);
        const auto bytesToRead = (int) (totalFloats * sizeof (float));
        if (f32Stream.read (interleaved.data(), bytesToRead) != bytesToRead)
            return false;

        // 重なる古い窓を削る
        removeOverlappingRanges (workingWindows, { meta.startFrame, meta.startFrame + meta.frames });

        // デインターリーブして格納
        auto pcm = std::make_shared<juce::AudioBuffer<float>> (numChannels, meta.frames);
        for (int ch = 0; ch < numChannels; ++ch)
        {
            auto* dest = pcm->getWritePointer (ch);
            const auto* src = interleaved.data() + ch;
            for (int i = 0; i < meta.frames; ++i)
                dest[i] = src[(size_t) i * (size_t) numChannels];
        }

        EditedWindow newWin;
        newWin.startFrame = meta.startFrame;
        newWin.numSamples = meta.frames;
        newWin.pcm = std::move (pcm);
        workingWindows.push_back (std::move (newWin));
    }

    // 3. startFrame 順にソート（削った後なので重ならない）
    std::sort (workingWindows.begin(), workingWindows.end(),
               [] (const EditedWindow& a, const EditedWindow& b) { return a.startFrame < b.startFrame; });

    // 4. 新しいスナップショットを構築して差し替え
    publish (std::make_shared<EditedPcmSnapshot> (rev, sampleRate, numChannels, std::move (workingWindows)));
    return true;
}

bool EditedPcm::applyDirty (const juce::String& rev,
                            bool reset,
                            const std::vector<juce::Range<juce::int64>>& restore,
                            const std::vector<WindowMeta>& windows,
                            const juce::File& f32File)
{
    if (windows.empty())
    {
        // 窓の追加が無い場合でも restore の適用や reset は行う
        juce::MemoryInputStream emptyStream (nullptr, 0, false);
        return applyDirty (rev, reset, restore, windows, emptyStream);
    }

    std::unique_ptr<juce::FileInputStream> stream (f32File.createInputStream());
    if (stream == nullptr || stream->failedToOpen())
        return false;

    return applyDirty (rev, reset, restore, windows, *stream);
}

bool EditedPcm::applyDirty (const juce::String& rev,
                            bool reset,
                            const std::vector<juce::Range<juce::int64>>& restore,
                            const std::vector<WindowMeta>& windows,
                            const float* f32Data,
                            size_t totalFloats)
{
    juce::MemoryInputStream stream (f32Data, totalFloats * sizeof (float), false);
    return applyDirty (rev, reset, restore, windows, stream);
}

} // namespace gliss
