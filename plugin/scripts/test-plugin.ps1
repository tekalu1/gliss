# Build Gliss.vst3 and run the automatic checks (see docs/ara-plugin.md).
#
#   powershell -File plugin\scripts\test-plugin.ps1            # build + all checks
#   powershell -File plugin\scripts\test-plugin.ps1 -SkipBuild # checks only
#
# Checks (each one has a timeout and its process tree is killed at the end):
#   1. ARA SDK TestHost, all test cases, on Gliss.vst3
#   2. ARA SDK TestHost, PlaybackRendering, with a render trace compared against the SDK test signal
#   3. GlissHostCheck (JUCE host: description, ARA factory, passthrough, editor without ARA shows the notice, teardown)
#   4. GlissPluginTests (unit tests, category "Gliss")
#   5. GlissHostCheck --editor: the editor's page (app/renderer, embedded) on a fake DocumentBridge, in-process and off-screen:
#      ui-ready, native functions, /fs/, events, keys, open/close 20 times with 2 editors
#   6. the same with GLISS_PLUGIN_WEB_DIR pointing at a marked copy of app/renderer (the page is read from the folder)
# Nothing is written outside plugin\build and the temp folder; the plug-in is never installed.
param(
    [switch]$SkipBuild,
    [string]$Config = 'Release',
    [string]$Python = 'python',
    [int]$TimeoutSec = 180
)

$ErrorActionPreference = 'Stop'
$plugin = Split-Path -Parent $PSScriptRoot
$build = Join-Path $plugin 'build'
$deps = Join-Path $build '_deps'
$exBuild = Join-Path $build 'ara-examples'
$vst3Sdk = Join-Path $deps 'vst3sdk-3.7.11'
# The ARA SDK is in _deps unless the build was configured with -DFETCHCONTENT_SOURCE_DIR_ARA_SDK=<existing copy>
$araSdk = Join-Path $deps 'ara_sdk-src'
$cache = Join-Path $build 'CMakeCache.txt'
if (Test-Path $cache) {
    $m = Select-String -Path $cache -Pattern '^FETCHCONTENT_SOURCE_DIR_ARA_SDK:[A-Z]+=(.+)$' | Select-Object -First 1
    if ($m -and $m.Matches[0].Groups[1].Value.Trim()) { $araSdk = $m.Matches[0].Groups[1].Value.Trim() }
}
$work = Join-Path ([System.IO.Path]::GetTempPath()) ('gliss-ara-test-' + [System.Diagnostics.Process]::GetCurrentProcess().Id)
New-Item -ItemType Directory -Force $work | Out-Null

function Invoke-Checked([string]$Exe, [string[]]$ExeArgs, [string]$Name, [hashtable]$Env = @{}) {
    # Run with a timeout; stdout+stderr go to $work\<Name>.log. Returns the exit code (-999 on timeout).
    $log = Join-Path $work ($Name + '.log')
    $saved = @{}
    foreach ($k in $Env.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, $Env[$k]) }
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList $ExeArgs -PassThru -NoNewWindow `
            -RedirectStandardOutput $log -RedirectStandardError ($log + '.err')
        $null = $p.Handle   # keep the handle so ExitCode is readable after exit
        if (-not $p.WaitForExit($TimeoutSec * 1000)) {
            & taskkill /T /F /PID $p.Id | Out-Null
            return -999
        }
        return $p.ExitCode
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
    }
}

$failed = @()
function Report([string]$Name, [bool]$Ok, [string]$Detail = '') {
    $mark = if ($Ok) { 'PASS' } else { 'FAIL' }
    Write-Host ("{0} {1} {2}" -f $mark, $Name, $Detail)
    if (-not $Ok) { $script:failed += $Name }
}

try {
    if (-not $SkipBuild) {
        cmake -S $plugin -B $build -G 'Visual Studio 17 2022' -A x64 -Wno-dev | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'cmake configure (plugin) failed' }
        cmake --build $build --config $Config --target GlissARA_VST3 GlissHostCheck GlissPluginTests --parallel 8 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'cmake build (plugin) failed' }

        # ARA SDK examples (TestHost). They need the VST3 SDK that the ARA SDK installs with its own script.
        if (-not (Test-Path (Join-Path $vst3Sdk 'cmake'))) {
            cmake "-DVST3_SDK_DIR=$vst3Sdk" -P (Join-Path $araSdk 'install_vst3sdk.cmake') | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'installing the VST3 SDK for the ARA examples failed' }
        }
        cmake -S (Join-Path $araSdk 'ARA_Examples') -B $exBuild -G 'Visual Studio 17 2022' -A x64 -Wno-dev `
            "-DARA_VST3_SDK_DIR=$vst3Sdk" -DARA_SETUP_DEBUGGING=OFF -DSMTG_CREATE_PLUGIN_LINK=OFF | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'cmake configure (ARA examples) failed' }
        cmake --build $exBuild --config $Config --target ARATestHost --parallel 8 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'cmake build (ARA TestHost) failed' }
    }

    $testHost = Join-Path $exBuild "bin\$Config\ARATestHost.exe"
    $gliss = Join-Path $build "GlissARA_artefacts\$Config\VST3\Gliss.vst3\Contents\x86_64-win\Gliss.vst3"
    $hostCheck = Join-Path $build "tests\hostcheck\GlissHostCheck_artefacts\$Config\GlissHostCheck.exe"
    $unitTests = Join-Path $build "tests\unit\GlissPluginTests_artefacts\$Config\GlissPluginTests.exe"
    foreach ($f in @($testHost, $gliss, $hostCheck, $unitTests)) { if (-not (Test-Path $f)) { throw "missing: $f" } }

    # 1. TestHost, all test cases
    $code = Invoke-Checked $testHost @('-vst3', $gliss) 'testhost-all'
    $log = Get-Content (Join-Path $work 'testhost-all.log.err'), (Join-Path $work 'testhost-all.log') -ErrorAction SilentlyContinue
    $ran = ($log | Select-String -SimpleMatch '*** testing').Count
    Report 'ARA TestHost (all test cases)' ($code -eq 0 -and $ran -ge 12) "exit=$code testCases=$ran"

    # 2. TestHost, PlaybackRendering, trace compared with the SDK test signal.
    #    TestHost renders at CPU speed in "realtime" mode, so wait for the prefetch (GLISS_ARA_READ_TIMEOUT_MS)
    #    to get complete blocks to compare. Without it, unfinished reads are zero-filled and skipped by the checker.
    $trace = Join-Path $work 'trace'
    New-Item -ItemType Directory -Force $trace | Out-Null
    $code = Invoke-Checked $testHost @('-vst3', $gliss, '-test', 'PlaybackRendering') 'testhost-render' `
        @{ GLISS_ARA_TRACE_DIR = $trace; GLISS_ARA_READ_TIMEOUT_MS = '3000' }
    $verify = & $Python (Join-Path $plugin 'tests\verify_render_trace.py') $trace
    Report 'ARA TestHost (PlaybackRendering, trace vs. SDK signal)' ($code -eq 0 -and $LASTEXITCODE -eq 0) "exit=$code $verify"

    # 3. JUCE host check
    $hcTrace = Join-Path $work 'hostcheck-trace'
    New-Item -ItemType Directory -Force $hcTrace | Out-Null
    $report = Join-Path $work 'hostcheck-report.txt'
    $code = Invoke-Checked $hostCheck @($report, $gliss, $hcTrace) 'hostcheck' @{ GLISS_ARA_TRACE_DIR = $hcTrace }
    $summary = (Get-Content $report -ErrorAction SilentlyContinue | Select-String 'RESULT').Line
    Report 'GlissHostCheck' ($code -eq 0) "exit=$code $summary"
    if ($code -ne 0 -and (Test-Path $report)) { Get-Content $report | Write-Host }

    # 4. unit tests
    $code = Invoke-Checked $unitTests @('Gliss') 'unit-tests'
    $summary = (Get-Content (Join-Path $work 'unit-tests.log') -ErrorAction SilentlyContinue | Select-String 'GlissPluginTests:').Line
    Report 'GlissPluginTests' ($code -eq 0) "exit=$code $summary"

    # 5. editor bridge (embedded page)
    $report = Join-Path $work 'editor-report.txt'
    $code = Invoke-Checked $hostCheck @('--editor', $report) 'editor' @{ GLISS_ARA_TRACE_DIR = $hcTrace }
    $summary = (Get-Content $report -ErrorAction SilentlyContinue | Select-String 'RESULT').Line
    Report 'GlissHostCheck --editor' ($code -eq 0) "exit=$code $summary"
    if ($code -ne 0 -and (Test-Path $report)) { Get-Content $report | Write-Host }

    # 6. editor bridge, page read from GLISS_PLUGIN_WEB_DIR (a copy of app/renderer with a marker)
    $webDir = Join-Path $work 'webdir'
    Copy-Item -Recurse -Force (Join-Path (Split-Path -Parent $plugin) 'app\renderer') $webDir
    $index = Join-Path $webDir 'index.html'
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $html = [System.IO.File]::ReadAllText($index, $utf8).Replace('<head>', '<head><meta name="gliss-test-marker" content="1">')
    [System.IO.File]::WriteAllText($index, $html, $utf8)
    $report = Join-Path $work 'editor-webdir-report.txt'
    $code = Invoke-Checked $hostCheck @('--editor', $report, '--expect-web-dir', '--cycles', '2') 'editor-webdir' `
        @{ GLISS_ARA_TRACE_DIR = $hcTrace; GLISS_PLUGIN_WEB_DIR = $webDir }
    $summary = (Get-Content $report -ErrorAction SilentlyContinue | Select-String 'RESULT').Line
    Report 'GlissHostCheck --editor (GLISS_PLUGIN_WEB_DIR)' ($code -eq 0) "exit=$code $summary"
    if ($code -ne 0 -and (Test-Path $report)) { Get-Content $report | Write-Host }
}
finally {
    # nothing we started may remain
    $left = Get-Process -Name ARATestHost, GlissHostCheck, GlissPluginTests -ErrorAction SilentlyContinue
    foreach ($p in $left) { & taskkill /T /F /PID $p.Id | Out-Null }
    Report 'no leftover test processes' (-not $left) ("killed=" + @($left).Count)

    # WebView2 browser processes of the editor (user data folder %TEMP%\GlissARA-<pid>) end shortly after the host quits
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $webviews = @(Get-CimInstance Win32_Process -Filter "Name = 'msedgewebview2.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'GlissARA-\d+' })
        if ($webviews.Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    foreach ($w in $webviews) { & taskkill /T /F /PID $w.ProcessId | Out-Null }
    Report 'no leftover WebView2 processes' ($webviews.Count -eq 0) ("killed=" + $webviews.Count)
    Write-Host "logs: $work"
}

if ($failed.Count -gt 0) { Write-Host ("FAILED: " + ($failed -join ', ')); exit 1 }
Write-Host 'ALL PASSED'
exit 0
