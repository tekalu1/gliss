#pragma once

#include <juce_core/juce_core.h>

#include <cstddef>
#include <functional>
#include <optional>
#include <vector>

namespace gliss::editor
{

/** WebView2 の resource provider が返す 1 つの資源（juce::WebBrowserComponent::Resource と同じ形。
    juce_gui_extra に頼らず単体テストで確かめられるよう、ここでは自前の型で持つ）。 */
struct WebResource
{
    std::vector<std::byte> data;
    juce::String mimeType;
};

/** 見つからない・読ませない資源の MIME。JUCE 9.0.3 の resource provider は状態コードを選べず（いつも 200）、
    資源を返さないと WebView2 がネットワークへ取りに行くので、404 の代わりにこの MIME の空でない本文を返す。
    画面（app/renderer/ara-bridge.js の fetchFs）はこの MIME を「読み取りを許していない場所」として扱う。
    ES モジュールとしては MIME が合わないので、/<名前>.js で返しても読み込みが失敗する。 */
inline constexpr const char* notFoundMimeType = "application/x-gliss-not-found";

/** エディタの画面（app/renderer）に配る資源を、要求のパスから決める。

    | パス | 中身 |
    |---|---|
    | `/`・`/index.html` | app/renderer/index.html |
    | `/<名前>.js` | app/renderer/<名前>.js（名前は英数字・`_`・`-`・`.` だけ。`/` を含まない） |
    | `/juce/index.js` | JUCE 9.0.3 の webview-interop の dist/index.js（`getNativeFunction`） |
    | `/fs/<encodeURIComponent(絶対パス)>` | ディスクのファイル。policy（DocumentBridge::isReadableByEditor）が許した通常のファイルだけ |

    開発時（環境変数 GLISS_PLUGIN_WEB_DIR が既にあるフォルダを指す）は、画面の資源を要求のたびにそのフォルダから読む
    （ビルドし直さずに画面を直せる）。`/juce/index.js` はいつも埋め込みのもの。

    メッセージスレッドから呼ぶ（policy もそこで呼ぶ）。 */
class WebResources
{
public:
    /** 埋め込みの資源をファイル名（`index.html`・`main.js` など）で引く。無ければ nullopt。 */
    using Lookup = std::function<std::optional<juce::MemoryBlock> (const juce::String& fileName)>;

    /** /fs/ で読ませてよいファイルか。 */
    using FsPolicy = std::function<bool (const juce::File&)>;

    /** pageAssets: app/renderer の埋め込み。juceInterop: `/juce/index.js` の中身。
        devFolder: 空でなければ画面の資源をここから読む（devFolderFromEnvironment()）。 */
    WebResources (Lookup pageAssets, juce::MemoryBlock juceInterop, juce::File devFolder, FsPolicy fsPolicy);

    /** 要求のパス（`https://juce.backend` の後ろ。`?`・`#` 以降は無視）に答える。見つからなければ notFound()。 */
    WebResource get (const juce::String& requestPath) const;

    /** withUserScript に渡す ara-bridge.js の全文（開発時はフォルダから、その都度読む）。読めなければ空。 */
    juce::String bridgeScript() const;

    /** 開発時のフォルダから読んでいるか。 */
    bool isServingFromFolder() const { return devFolder != juce::File(); }

    // ---- 単体テストからも使う部品 ----

    /** GLISS_PLUGIN_WEB_DIR が既にあるフォルダを指していればそれ、無ければ空の File。 */
    static juce::File devFolderFromEnvironment();

    /** `/fs/` の後ろ（encodeURIComponent した絶対パス）を 1 回だけ percent-decode し、絶対パスの File にする。
        `+` は空白にしない（encodeURIComponent は `+` を `%2B` にする）。壊れた `%`・UTF-8 でない並び・
        制御文字・相対パス・`..` の段を含むものは nullopt。 */
    static std::optional<juce::File> decodeFsPath (const juce::String& encoded);

    /** `/<名前>.js` に使ってよい名前か。 */
    static bool isPageScriptName (const juce::String& name);

    /** 拡張子から MIME（.html・.js・.json・.css・.svg・.wav、ほかは application/octet-stream）。 */
    static juce::String mimeTypeFor (const juce::String& fileName);

    static WebResource notFound();

private:
    std::optional<WebResource> pageAsset (const juce::String& fileName) const;
    std::optional<WebResource> diskFile (const juce::String& encodedPath) const;

    Lookup pageAssets;
    juce::MemoryBlock juceInterop;
    juce::File devFolder;
    FsPolicy fsPolicy;
};

} // namespace gliss::editor
