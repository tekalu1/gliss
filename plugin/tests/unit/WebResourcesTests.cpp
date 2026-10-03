// エディタの resource provider（src/editor/WebResources）。WebView2 を使わずに、要求のパス → 返す資源を確かめる。
#include "editor/WebResources.h"

namespace
{
using gliss::editor::WebResource;
using gliss::editor::WebResources;

juce::String asText (const WebResource& r)
{
    return juce::String::fromUTF8 (reinterpret_cast<const char*> (r.data.data()), (int) r.data.size());
}

bool isNotFound (const WebResource& r)
{
    return r.mimeType == gliss::editor::notFoundMimeType;
}

/** JS の encodeURIComponent と同じ（英数字と - _ . ! ~ * ' ( ) 以外を UTF-8 の %XX にする）。 */
juce::String encodeURIComponent (const juce::String& s)
{
    juce::String out;
    const auto* p = s.toRawUTF8();

    for (size_t i = 0; i < s.getNumBytesAsUTF8(); ++i)
    {
        const auto c = (unsigned char) p[i];

        if (juce::CharacterFunctions::isLetterOrDigit ((char) c) && c < 0x80)
            out << (char) c;
        else if (juce::String ("-_.!~*'()").containsChar ((juce::juce_wchar) c))
            out << (char) c;
        else
            out << "%" << juce::String::toHexString ((int) c).paddedLeft ('0', 2).toUpperCase();
    }

    return out;
}

juce::MemoryBlock block (const juce::String& text)
{
    return { text.toRawUTF8(), text.getNumBytesAsUTF8() };
}

class WebResourcesTests final : public juce::UnitTest
{
public:
    WebResourcesTests() : juce::UnitTest ("Editor web resources", "Gliss") {}

    void runTest() override
    {
        const auto root = juce::File::getSpecialLocation (juce::File::tempDirectory)
                              .getChildFile ("gliss-webresources-test-" + juce::String (juce::Random::getSystemRandom().nextInt64()));
        const auto work = root.getChildFile ("work").getChildFile ("ara").getChildFile ("k1");
        work.createDirectory();
        struct Cleanup { juce::File dir; ~Cleanup() { dir.deleteRecursively(); } } cleanup { root };

        const auto allowUnderWork = [work] (const juce::File& f) { return f.isAChildOf (work); };
        const auto embedded = [] (const juce::String& fileName) -> std::optional<juce::MemoryBlock>
        {
            if (fileName == "index.html")    return block ("<html>embedded</html>");
            if (fileName == "main.js")       return block ("export const where = 'embedded';");
            if (fileName == "ara-bridge.js") return block ("// bridge (embedded)");
            return std::nullopt;
        };

        beginTest ("decodeFsPath decodes once");
        {
            const auto path = juce::String::fromUTF8 ("D:\\Gliss\\work\\ara\\k1\\view data %+日本語.json");
            const auto encoded = encodeURIComponent (path);
            expectEquals (encoded, juce::String ("D%3A%5CGliss%5Cwork%5Cara%5Ck1%5Cview%20data%20%25%2B%E6%97%A5%E6%9C%AC%E8%AA%9E.json"));
            const auto decoded = WebResources::decodeFsPath (encoded);
            expect (decoded.has_value());
            expectEquals (decoded->getFullPathName(), path);

            // %25 は 1 回だけ解く（%2525 → %25）。+ は空白にしない
            expectEquals (WebResources::decodeFsPath ("C%3A%5Ca%2525b+c.json")->getFullPathName(), juce::String ("C:\\a%25b+c.json"));
        }

        beginTest ("decodeFsPath refuses broken and unsafe paths");
        {
            for (auto* bad : { "", "%", "%4", "%zz", "C%3A%5Ca%ZZ.json", "relative%5Cpath.json", "%C3%28.json",
                               "C%3A%5Ca%5C..%5Cb.json", "C%3A%5Ca%5C.%5Cb.json", "C%3A%2F..%2Fb.json",
                               "C%3A%5Ca.json%3Astream", "%5C%5C%3F%5CC%3A%5Ca.json", "%5C%5C.%5Cpipe%5Cx",
                               "C%3A%5Ca%0Ab.json" })
                expect (! WebResources::decodeFsPath (bad).has_value(), juce::String ("should refuse: ") + bad);
        }

        beginTest ("page assets and the JUCE helper");
        {
            WebResources res (embedded, block ("export function getNativeFunction() {}"), {}, allowUnderWork);
            expect (! res.isServingFromFolder());

            for (auto* p : { "/", "", "/index.html", "/?x=1", "/index.html#top" })
            {
                const auto r = res.get (p);
                expectEquals (asText (r), juce::String ("<html>embedded</html>"), p);
                expect (r.mimeType.startsWith ("text/html"));
            }

            const auto main = res.get ("/main.js");
            expectEquals (asText (main), juce::String ("export const where = 'embedded';"));
            expect (main.mimeType.startsWith ("text/javascript"));

            const auto juce = res.get ("/juce/index.js");
            expect (asText (juce).contains ("getNativeFunction"));
            expect (juce.mimeType.startsWith ("text/javascript"));

            for (auto* p : { "/nope.js", "/juce/other.js", "/a/main.js", "/../main.js", "/%2E%2E/main.js", "/main.JS.map",
                             "/.hidden.js", "/main.css", "/fs/", "/fsx" })
                expect (isNotFound (res.get (p)), juce::String ("should be not found: ") + p);

            expectEquals (res.bridgeScript(), juce::String ("// bridge (embedded)"));
        }

        beginTest ("the development folder is read on every request");
        {
            const auto web = root.getChildFile ("web");
            web.createDirectory();
            web.getChildFile ("index.html").replaceWithText ("<html>folder v1</html>");
            web.getChildFile ("ara-bridge.js").replaceWithText ("// bridge (folder)");

            WebResources res (embedded, block ("x"), web, allowUnderWork);
            expect (res.isServingFromFolder());
            expectEquals (asText (res.get ("/")), juce::String ("<html>folder v1</html>"));
            web.getChildFile ("index.html").replaceWithText ("<html>folder v2</html>");
            expectEquals (asText (res.get ("/")), juce::String ("<html>folder v2</html>"));
            expectEquals (res.bridgeScript(), juce::String ("// bridge (folder)"));

            // フォルダに無いものは埋め込みに戻らない（直した画面と古い画面が混ざらない）
            expect (isNotFound (res.get ("/main.js")));
            expectEquals (asText (res.get ("/juce/index.js")), juce::String ("x"));
        }

        beginTest ("/fs/ serves only what the policy allows");
        {
            const auto json = work.getChildFile (juce::String::fromUTF8 ("view data %+日本語.json"));
            json.replaceWithText (juce::String::fromUTF8 ("{\"hello\":\"世界\"}"));
            const auto bin = work.getChildFile ("wave.bin");
            bin.replaceWithData ("\x01\x02\x03", 3);
            const auto outside = root.getChildFile ("outside.json");
            outside.replaceWithText ("{}");

            int policyCalls = 0;
            WebResources res (embedded, {}, {}, [&] (const juce::File& f) { ++policyCalls; return allowUnderWork (f); });

            const auto r = res.get ("/fs/" + encodeURIComponent (json.getFullPathName()));
            expectEquals (r.mimeType, juce::String ("application/json"));
            expectEquals (asText (r), juce::String::fromUTF8 ("{\"hello\":\"世界\"}"));

            const auto b = res.get ("/fs/" + encodeURIComponent (bin.getFullPathName()) + "?cache=1");
            expectEquals (b.mimeType, juce::String ("application/octet-stream"));
            expectEquals ((int) b.data.size(), 3);

            const auto callsBefore = policyCalls;
            expect (isNotFound (res.get ("/fs/" + encodeURIComponent (outside.getFullPathName()))));
            expect (policyCalls == callsBefore + 1, "the policy decides for an existing file");

            expect (isNotFound (res.get ("/fs/" + encodeURIComponent (work.getChildFile ("missing.json").getFullPathName()))));
            expect (isNotFound (res.get ("/fs/" + encodeURIComponent (work.getFullPathName()))));   // フォルダ
            expect (isNotFound (res.get ("/fs/" + encodeURIComponent (work.getFullPathName() + "\\..\\..\\..\\outside.json"))));
            expect (isNotFound (res.get ("/fs/" + encodeURIComponent ("outside.json"))));

            WebResources noPolicy (embedded, {}, {}, nullptr);
            expect (isNotFound (noPolicy.get ("/fs/" + encodeURIComponent (json.getFullPathName()))));
        }

        beginTest ("page script names and MIME types");
        {
            for (auto* ok : { "main.js", "ara-bridge.js", "first-run.js", "a_b.c.js" })
                expect (WebResources::isPageScriptName (ok), ok);

            for (auto* bad : { ".js", "x.js/", "../x.js", "a/b.js", "a\\b.js", "x.mjs", "x js.js", "%2e.js", ".x.js", "a..b.js" })
                expect (! WebResources::isPageScriptName (bad), bad);

            expectEquals (WebResources::mimeTypeFor ("a.JSON"), juce::String ("application/json"));
            expectEquals (WebResources::mimeTypeFor ("a.wav"), juce::String ("audio/wav"));
            expectEquals (WebResources::mimeTypeFor ("a"), juce::String ("application/octet-stream"));
        }
    }
};

WebResourcesTests webResourcesTests;
}
