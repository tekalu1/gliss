#include "WebResources.h"

namespace gliss::editor
{

namespace
{
constexpr juce::int64 maxDiskFileBytes = 512 * 1024 * 1024;   // 画面が読むのは数 MB の JSON と波形。桁違いのものは断る

WebResource fromMemory (const juce::MemoryBlock& block, const juce::String& mimeType)
{
    const auto* bytes = static_cast<const std::byte*> (block.getData());
    return { std::vector<std::byte> (bytes, bytes + block.getSize()), mimeType };
}

int hexValue (juce::juce_wchar c)
{
    if (c >= '0' && c <= '9') return (int) (c - '0');
    if (c >= 'a' && c <= 'f') return (int) (c - 'a') + 10;
    if (c >= 'A' && c <= 'F') return (int) (c - 'A') + 10;
    return -1;
}
}

WebResources::WebResources (Lookup pageAssetsIn, juce::MemoryBlock juceInteropIn, juce::File devFolderIn, FsPolicy fsPolicyIn)
    : pageAssets (std::move (pageAssetsIn)),
      juceInterop (std::move (juceInteropIn)),
      devFolder (std::move (devFolderIn)),
      fsPolicy (std::move (fsPolicyIn))
{
}

WebResource WebResources::get (const juce::String& requestPath) const
{
    auto path = requestPath.upToFirstOccurrenceOf ("?", false, false).upToFirstOccurrenceOf ("#", false, false);

    if (path.isEmpty() || path == "/" || path == "/index.html")
        return pageAsset ("index.html").value_or (notFound());

    if (path.startsWith ("/fs/"))
        return diskFile (path.substring (4)).value_or (notFound());

    if (path == "/juce/index.js")
        return juceInterop.isEmpty() ? notFound() : fromMemory (juceInterop, mimeTypeFor ("index.js"));

    if (const auto name = path.substring (1); path.startsWith ("/") && isPageScriptName (name))
        return pageAsset (name).value_or (notFound());

    return notFound();
}

juce::String WebResources::bridgeScript() const
{
    const juce::String name ("ara-bridge.js");

    if (devFolder != juce::File())
        return devFolder.getChildFile (name).loadFileAsString();

    if (const auto block = pageAssets != nullptr ? pageAssets (name) : std::nullopt)
        return juce::String::fromUTF8 (static_cast<const char*> (block->getData()), (int) block->getSize());

    return {};
}

std::optional<WebResource> WebResources::pageAsset (const juce::String& fileName) const
{
    if (devFolder != juce::File())
    {
        const auto file = devFolder.getChildFile (fileName);
        juce::MemoryBlock block;

        if (file.existsAsFile() && file.loadFileAsData (block))
            return fromMemory (block, mimeTypeFor (fileName));

        return std::nullopt;
    }

    if (pageAssets != nullptr)
        if (const auto block = pageAssets (fileName))
            return fromMemory (*block, mimeTypeFor (fileName));

    return std::nullopt;
}

std::optional<WebResource> WebResources::diskFile (const juce::String& encodedPath) const
{
    const auto file = decodeFsPath (encodedPath);

    if (! file.has_value() || fsPolicy == nullptr)
        return std::nullopt;

    // リンク（シンボリックリンク・ジャンクション）の先は辿らない。作業場所の下かどうかは policy が正規化したパスで決める
    if (! file->existsAsFile() || file->isSymbolicLink() || file->getSize() > maxDiskFileBytes || ! fsPolicy (*file))
        return std::nullopt;

    juce::MemoryBlock block;

    if (! file->loadFileAsData (block))
        return std::nullopt;

    return fromMemory (block, mimeTypeFor (file->getFileName()));
}

juce::File WebResources::devFolderFromEnvironment()
{
    const auto value = juce::SystemStats::getEnvironmentVariable ("GLISS_PLUGIN_WEB_DIR", {}).trim();

    if (value.isEmpty() || ! juce::File::isAbsolutePath (value))
        return {};

    const juce::File folder (value);
    return folder.isDirectory() ? folder : juce::File();
}

std::optional<juce::File> WebResources::decodeFsPath (const juce::String& encoded)
{
    // 1 回だけ解く。URL::removeEscapeChars は先に + を空白にするので使わない
    juce::MemoryOutputStream bytes;

    for (auto p = encoded.getCharPointer(); ! p.isEmpty();)
    {
        const auto c = p.getAndAdvance();

        if (c == '%')
        {
            const auto hi = hexValue (p.getAndAdvance());
            const auto lo = hi >= 0 ? hexValue (p.getAndAdvance()) : -1;

            if (hi < 0 || lo < 0)
                return std::nullopt;

            bytes.writeByte ((char) ((hi << 4) | lo));
        }
        else
        {
            bytes.appendUTF8Char (c);
        }
    }

    const auto* data = static_cast<const char*> (bytes.getData());
    const auto size = bytes.getDataSize();

    if (size == 0 || ! juce::CharPointer_UTF8::isValidString (data, (int) size))
        return std::nullopt;

    const auto path = juce::String::fromUTF8 (data, (int) size);

    for (auto p = path.getCharPointer(); ! p.isEmpty();)
        if (const auto c = p.getAndAdvance(); c < 0x20 || c == 0x7f)
            return std::nullopt;

    // 装置のパス（\\?\・\\.\）・代替データストリーム（2 文字目より後ろの :）・. と .. の段は断る
    if (path.startsWith ("\\\\?\\") || path.startsWith ("\\\\.\\") || path.lastIndexOfChar (':') > 1)
        return std::nullopt;

    for (const auto& part : juce::StringArray::fromTokens (path, "\\/", {}))
        if (part == "." || part == "..")
            return std::nullopt;

    if (! juce::File::isAbsolutePath (path))
        return std::nullopt;

    return juce::File (path);
}

bool WebResources::isPageScriptName (const juce::String& name)
{
    if (name.length() < 4 || name.length() > 128 || ! name.endsWith (".js") || name.startsWithChar ('.') || name.contains (".."))
        return false;

    return name.containsOnly ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.");
}

juce::String WebResources::mimeTypeFor (const juce::String& fileName)
{
    const auto ext = fileName.fromLastOccurrenceOf (".", false, false).toLowerCase();

    if (ext == "html" || ext == "htm") return "text/html; charset=utf-8";
    if (ext == "js" || ext == "mjs")   return "text/javascript; charset=utf-8";
    if (ext == "json")                 return "application/json";
    if (ext == "css")                  return "text/css; charset=utf-8";
    if (ext == "svg")                  return "image/svg+xml";
    if (ext == "wav")                  return "audio/wav";
    return "application/octet-stream";
}

WebResource WebResources::notFound()
{
    static const juce::String body ("not found");
    const auto* bytes = reinterpret_cast<const std::byte*> (body.toRawUTF8());
    return { std::vector<std::byte> (bytes, bytes + body.getNumBytesAsUTF8()), notFoundMimeType };
}

} // namespace gliss::editor
