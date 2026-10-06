#include "ArchiveIO.h"

namespace gliss::archive
{

juce::String write (const DocumentArchive& a)
{
    auto* document = new juce::DynamicObject();
    document->setProperty ("work_key", a.workKey);
    document->setProperty ("guide", a.guide.isNotEmpty() ? juce::var (a.guide) : juce::var());

    if (! a.guides.empty() || ! a.modifications.empty())
    {
        auto* guides = new juce::DynamicObject();

        for (const auto& [id, guideId] : a.guides)
            guides->setProperty (juce::Identifier (id), guideId);

        document->setProperty ("guides", juce::var (guides));
    }

    auto* modifications = new juce::DynamicObject();

    for (const auto& [id, m] : a.modifications)
    {
        auto* entry = new juce::DynamicObject();
        entry->setProperty ("name", m.name);
        entry->setProperty ("archive", m.archive.isObject() ? m.archive : juce::var());
        modifications->setProperty (juce::Identifier (id), juce::var (entry));
    }

    auto* root = new juce::DynamicObject();
    root->setProperty ("format", formatName);
    root->setProperty ("version", formatVersion);
    root->setProperty ("document", juce::var (document));
    root->setProperty ("modifications", juce::var (modifications));

    return juce::JSON::toString (juce::var (root), true);
}

bool read (const juce::String& json, DocumentArchive& out, juce::String& error)
{
    const auto root = juce::JSON::parse (json);

    if (! root.isObject() || root.getProperty ("format", {}).toString() != formatName)
    {
        error = "not a Gliss archive";
        return false;
    }

    const auto version = root.getProperty ("version", {});

    if (! (version.isInt() || version.isInt64()) || (int) version < 1 || (int) version > formatVersion)
    {
        error = "unsupported version " + version.toString();
        return false;
    }

    out = {};
    const auto document = root.getProperty ("document", {});
    out.workKey = document.getProperty ("work_key", {}).toString();

    if (const auto guide = document.getProperty ("guide", {}); guide.isString())
        out.guide = guide.toString();

    if (auto* guides = document.getProperty ("guides", {}).getDynamicObject())
    {
        out.hasGuides = true;

        for (const auto& property : guides->getProperties())
            if (property.value.isString() && property.value.toString().isNotEmpty())
                out.guides[property.name.toString()] = property.value.toString();
    }

    if (auto* modifications = root.getProperty ("modifications", {}).getDynamicObject())
    {
        for (const auto& property : modifications->getProperties())
        {
            ModificationArchive m;
            m.name = property.value.getProperty ("name", {}).toString();

            if (const auto a = property.value.getProperty ("archive", {}); a.isObject())
                m.archive = a;

            out.modifications[property.name.toString()] = std::move (m);
        }
    }

    return true;
}

juce::String makeWorkKey()
{
    return juce::Uuid().toDashedString();
}

bool isValidWorkKey (const juce::String& key)
{
    if (key.isEmpty() || key.length() > 64)
        return false;

    for (auto c : key)
        if (! (juce::CharacterFunctions::isLetterOrDigit (c) && c < 128) && c != '-' && c != '_')
            return false;

    return true;
}

juce::String sourceKey (const juce::String& persistentId)
{
    juce::uint64 hash = 14695981039346656037ull;

    for (auto* p = persistentId.toRawUTF8(); *p != 0; ++p)
    {
        hash ^= (juce::uint8) *p;
        hash *= 1099511628211ull;
    }

    return juce::String::toHexString ((juce::int64) hash).paddedLeft ('0', 16);
}

} // namespace gliss::archive
