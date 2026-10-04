// GlissPluginTests の入口。JUCE の UnitTest のカテゴリ "Gliss"（引数でほかのカテゴリ、"all" で JUCE 自身のものも全部）を流し、失敗があれば 1 を返す。
// Gliss の単位のテストは juce::UnitTest ("<名前>", "Gliss") で登録する。
#include <juce_core/juce_core.h>
#include <juce_events/juce_events.h>

int main (int argc, char* argv[])
{
    juce::ScopedJuceInitialiser_GUI init;
    juce::UnitTestRunner runner;
    runner.setAssertOnFailure (false);

    const auto category = argc > 1 ? juce::String::fromUTF8 (argv[1]) : juce::String ("Gliss");
    if (category == "all")
        runner.runAllTests();
    else
        runner.runTestsInCategory (category);

    int failures = 0;
    for (int i = 0; i < runner.getNumResults(); ++i)
        failures += runner.getResult (i)->failures;

    std::printf ("GlissPluginTests: %d results, %d failures\n", runner.getNumResults(), failures);
    return failures == 0 ? 0 : 1;
}
