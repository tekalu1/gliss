#include "EditedPcm.h"

#include <algorithm>

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

void EditedPcmSnapshot::getOverlappingWindows (juce::int64 startFrame,
                                              int numFrames,
                                              std::vector<const EditedWindow*>& result) const
{
    result.clear();
    if (numFrames <= 0 || windows.empty())
        return;

    const auto reqEnd = startFrame + numFrames;
    for (const auto& w : windows)
    {
        if (w.startFrame >= reqEnd)
            break;
        if (w.getEndFrame() > startFrame)
            result.push_back (&w);
    }
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
    const juce::SpinLock::ScopedLockType lock (spinLock);
    return currentSnapshot != nullptr ? currentSnapshot->getRev() : juce::String();
}

void EditedPcm::setFormat (double newSampleRate, int newNumChannels)
{
    const juce::SpinLock::ScopedLockType lock (spinLock);
    sampleRate = newSampleRate;
    numChannels = newNumChannels;
    currentSnapshot = std::make_shared<EditedPcmSnapshot> (juce::String(), sampleRate, numChannels, std::vector<EditedWindow>());
}

void EditedPcm::clear()
{
    const juce::SpinLock::ScopedLockType lock (spinLock);
    currentSnapshot = std::make_shared<EditedPcmSnapshot> (juce::String(), sampleRate, numChannels, std::vector<EditedWindow>());
}

void EditedPcm::removeOverlappingRanges (std::vector<EditedWindow>& list,
                                         juce::Range<juce::int64> rangeToRemove)
{
    if (rangeToRemove.isEmpty())
        return;

    const auto rStart = rangeToRemove.getStart();
    const auto rEnd = rangeToRemove.getEnd();

    std::vector<EditedWindow> result;
    result.reserve (list.size() + 2);

    for (auto& w : list)
    {
        const auto wStart = w.startFrame;
        const auto wEnd = w.getEndFrame();

        // 重なりなし
        if (rEnd <= wStart || rStart >= wEnd)
        {
            result.push_back (std::move (w));
            continue;
        }

        // 完全被覆（削除）
        if (rStart <= wStart && rEnd >= wEnd)
        {
            continue;
        }

        // 先頭側が削られる
        if (rStart <= wStart && rEnd < wEnd)
        {
            const auto cut = (int) (rEnd - wStart);
            const auto rem = w.getNumSamples() - cut;
            EditedWindow newWin;
            newWin.startFrame = rEnd;
            newWin.buffer.setSize (w.getNumChannels(), rem);
            for (int ch = 0; ch < w.getNumChannels(); ++ch)
                newWin.buffer.copyFrom (ch, 0, w.buffer, ch, cut, rem);
            result.push_back (std::move (newWin));
            continue;
        }

        // 末尾側が削られる
        if (rStart > wStart && rEnd >= wEnd)
        {
            const auto rem = (int) (rStart - wStart);
            EditedWindow newWin;
            newWin.startFrame = wStart;
            newWin.buffer.setSize (w.getNumChannels(), rem);
            for (int ch = 0; ch < w.getNumChannels(); ++ch)
                newWin.buffer.copyFrom (ch, 0, w.buffer, ch, 0, rem);
            result.push_back (std::move (newWin));
            continue;
        }

        // 中間が削られる（2分割）
        if (rStart > wStart && rEnd < wEnd)
        {
            const auto rem1 = (int) (rStart - wStart);
            EditedWindow newWin1;
            newWin1.startFrame = wStart;
            newWin1.buffer.setSize (w.getNumChannels(), rem1);
            for (int ch = 0; ch < w.getNumChannels(); ++ch)
                newWin1.buffer.copyFrom (ch, 0, w.buffer, ch, 0, rem1);
            result.push_back (std::move (newWin1));

            const auto offset2 = (int) (rEnd - wStart);
            const auto rem2 = (int) (wEnd - rEnd);
            EditedWindow newWin2;
            newWin2.startFrame = rEnd;
            newWin2.buffer.setSize (w.getNumChannels(), rem2);
            for (int ch = 0; ch < w.getNumChannels(); ++ch)
                newWin2.buffer.copyFrom (ch, 0, w.buffer, ch, offset2, rem2);
            result.push_back (std::move (newWin2));
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
    std::vector<EditedWindow> workingWindows;
    if (! reset)
    {
        const juce::SpinLock::ScopedLockType lock (spinLock);
        if (currentSnapshot != nullptr)
            workingWindows = currentSnapshot->getWindows();
    }

    // 1. restore 範囲を適用
    applyRestoreRanges (workingWindows, restore);

    // 2. windows を読み込んで追加
    for (const auto& meta : windows)
    {
        if (meta.frames <= 0)
            continue;

        if (! f32Stream.setPosition (meta.byteOffset))
            return false;

        const auto totalFloats = (size_t) meta.frames * (size_t) numChannels;
        std::vector<float> interleaved (totalFloats);
        const auto bytesToRead = (int) (totalFloats * sizeof (float));
        const auto bytesRead = f32Stream.read (interleaved.data(), bytesToRead);
        if (bytesRead != bytesToRead)
            return false;

        // 重複部分を除去
        juce::Range<juce::int64> winRange (meta.startFrame, meta.startFrame + meta.frames);
        removeOverlappingRanges (workingWindows, winRange);

        // デインターリーブして格納
        EditedWindow newWin;
        newWin.startFrame = meta.startFrame;
        newWin.buffer.setSize (numChannels, meta.frames);

        for (int i = 0; i < meta.frames; ++i)
        {
            for (int ch = 0; ch < numChannels; ++ch)
            {
                newWin.buffer.setSample (ch, i, interleaved[(size_t) i * (size_t) numChannels + (size_t) ch]);
            }
        }

        workingWindows.push_back (std::move (newWin));
    }

    // 3. startFrame 順にソート
    std::sort (workingWindows.begin(), workingWindows.end(),
               [] (const EditedWindow& a, const EditedWindow& b) { return a.startFrame < b.startFrame; });

    // 4. 新しいスナップショットを構築して差し替え
    auto nextSnapshot = std::make_shared<EditedPcmSnapshot> (rev, sampleRate, numChannels, std::move (workingWindows));
    {
        const juce::SpinLock::ScopedLockType lock (spinLock);
        currentSnapshot = std::move (nextSnapshot);
    }

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
