#include "GlissDocumentController.h"

#include "Diagnostics.h"
#include "GlissPlaybackRenderer.h"

namespace gliss
{

namespace
{
constexpr auto archiveFormatName = "gliss-ara-document";
}

GlissAudioModification::GlissAudioModification (juce::ARAAudioSource* audioSource,
                                                ARA::ARAAudioModificationHostRef hostRef,
                                                const juce::ARAAudioModification* optionalModificationToClone)
    : ARAAudioModification (audioSource, hostRef, optionalModificationToClone)
{
    if (optionalModificationToClone != nullptr)
        edits = static_cast<const GlissAudioModification*> (optionalModificationToClone)->edits;
}

//==============================================================================
void GlissDocumentController::willBeginEditing (juce::ARADocument*)
{
    processBlockLock.enterWrite();
}

void GlissDocumentController::didEndEditing (juce::ARADocument*)
{
    processBlockLock.exitWrite();
}

juce::ScopedTryReadLock GlissDocumentController::getProcessingLock()
{
    return juce::ScopedTryReadLock { processBlockLock };
}

juce::ARAAudioModification* GlissDocumentController::doCreateAudioModification (juce::ARAAudioSource* audioSource,
                                                                                ARA::ARAAudioModificationHostRef hostRef,
                                                                                const juce::ARAAudioModification* optionalModificationToClone) noexcept
{
    return new GlissAudioModification (audioSource, hostRef, optionalModificationToClone);
}

juce::ARAPlaybackRenderer* GlissDocumentController::doCreatePlaybackRenderer() noexcept
{
    return new GlissPlaybackRenderer (getDocumentController(), *this);
}

//==============================================================================
// アーカイブは「版つきの JSON 1 つ」。解析のキャッシュは入れない（編集の一覧だけ。plan.md「保存」）。
//   { "format": "gliss-ara-document", "version": 1,
//     "audioModifications": [ { "id": "<persistentID>", "edits": [] } ] }
bool GlissDocumentController::doStoreObjectsToStream (juce::ARAOutputStream& output, const juce::ARAStoreObjectsFilter* filter) noexcept
{
    const auto& modifications = filter->getAudioModificationsToStore<GlissAudioModification>();

    juce::Array<juce::var> entries;

    for (const auto* modification : modifications)
    {
        auto* entry = new juce::DynamicObject();
        entry->setProperty ("id", juce::String (modification->getPersistentID()));
        entry->setProperty ("edits", modification->getEdits());
        entries.add (juce::var (entry));
    }

    auto* root = new juce::DynamicObject();
    root->setProperty ("format", archiveFormatName);
    root->setProperty ("version", archiveFormatVersion);
    root->setProperty ("audioModifications", entries);

    const auto json = juce::JSON::toString (juce::var (root), true);
    diag::log ("archive: store " + juce::String (modifications.size()) + " audio modification(s), " + juce::String (json.length()) + " chars");

    return output.writeString (json);
}

bool GlissDocumentController::doRestoreObjectsFromStream (juce::ARAInputStream& input, const juce::ARARestoreObjectsFilter* filter) noexcept
{
    const auto json = input.readString();

    if (input.failed())
        return false;

    const auto root = juce::JSON::parse (json);

    if (! root.isObject() || root.getProperty ("format", {}).toString() != archiveFormatName)
    {
        diag::log ("archive: restore failed, not a Gliss archive");
        return false;
    }

    const int version = root.getProperty ("version", 0);

    if (version < 1 || version > archiveFormatVersion)
    {
        diag::log ("archive: restore failed, unsupported version " + juce::String (version));
        return false;
    }

    const auto* entries = root.getProperty ("audioModifications", {}).getArray();
    int restored = 0;

    if (entries != nullptr)
    {
        for (const auto& entry : *entries)
        {
            const auto id = entry.getProperty ("id", {}).toString();

            // フィルターが「戻さない」としたもの（別の素材に割り当て済み・対象外）は捨てる。
            if (auto* modification = filter->getAudioModificationToRestoreStateWithID<GlissAudioModification> (id.toRawUTF8()))
            {
                modification->setEdits (entry.getProperty ("edits", juce::Array<juce::var>()));
                ++restored;
            }
        }
    }

    diag::log ("archive: restore version " + juce::String (version) + ", "
               + juce::String (entries != nullptr ? entries->size() : 0) + " entr(ies), "
               + juce::String (restored) + " matched");
    return true;
}

} // namespace gliss
