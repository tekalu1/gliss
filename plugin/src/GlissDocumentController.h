#pragma once

#include <juce_audio_processors/juce_audio_processors.h>

namespace gliss
{

/** オーディオスレッドが、ドキュメントの編集中かどうかを待たずに確かめる口。 */
struct ProcessingLockInterface
{
    virtual ~ProcessingLockInterface() = default;
    virtual juce::ScopedTryReadLock getProcessingLock() = 0;
};

/** アーカイブの形の版。保存の形が下位互換でなくなったら上げ、ARA_DOCUMENT_ARCHIVE_ID も上げる。 */
constexpr int archiveFormatVersion = 1;

/** 編集の単位（plan.md「編集の単位」）。編集はソースの時間で持ち、同じ素材の複製・分割は編集を共有する。
    段階 1 では編集の中身はまだ無いので、空の配列を持つだけ。 */
class GlissAudioModification final : public juce::ARAAudioModification
{
public:
    GlissAudioModification (juce::ARAAudioSource* audioSource,
                            ARA::ARAAudioModificationHostRef hostRef,
                            const juce::ARAAudioModification* optionalModificationToClone);

    const juce::var& getEdits() const { return edits; }
    void setEdits (juce::var newEdits) { edits = std::move (newEdits); }

private:
    juce::var edits { juce::Array<juce::var>() };
};

/** DocumentController の実装。エンジンとの接続（段階 2）はここに 1 つだけ持たせる。 */
class GlissDocumentController final : public juce::ARADocumentControllerSpecialisation,
                                      private ProcessingLockInterface
{
public:
    using ARADocumentControllerSpecialisation::ARADocumentControllerSpecialisation;

protected:
    void willBeginEditing (juce::ARADocument*) override;
    void didEndEditing (juce::ARADocument*) override;

    juce::ARAAudioModification* doCreateAudioModification (juce::ARAAudioSource* audioSource,
                                                           ARA::ARAAudioModificationHostRef hostRef,
                                                           const juce::ARAAudioModification* optionalModificationToClone) noexcept override;
    juce::ARAPlaybackRenderer* doCreatePlaybackRenderer() noexcept override;

    bool doRestoreObjectsFromStream (juce::ARAInputStream& input, const juce::ARARestoreObjectsFilter* filter) noexcept override;
    bool doStoreObjectsToStream (juce::ARAOutputStream& output, const juce::ARAStoreObjectsFilter* filter) noexcept override;

private:
    juce::ScopedTryReadLock getProcessingLock() override;

    juce::ReadWriteLock processBlockLock;
};

} // namespace gliss
