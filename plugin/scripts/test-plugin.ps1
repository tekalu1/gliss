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
#   6b. GlissHostCheck --ara-playback: a real ARA document (JUCE ARA hosting) prepared once; then a region is added, the samples
#      access is switched off and on, and the host reports a change of the source's samples. The original sound keeps playing
#      each time (the renderer replaces its source readers and region table without prepareToPlay), and plugin.log has the
#      renderer lines (prepare, source reader replaced, regions synced, release)
#   6c. GlissHostCheck --ara-preview: two plug-in instances (editor renderers) bound to one ARA document; a preview asked for
#      by one of them (the plug-in's test bridge, "@preview", with the requester's editor renderer id) is added to that
#      instance's output only, also when no editor renderer covers the previewed modification and another instance owned
#      the previous preview
#   6d. GlissHostCheck --ara-preview-playback: the same, for a host that gives no region to any editor renderer (Studio Pro): only the
#      playback renderers have regions, and the instance that asked (the editor window's owner) is not the one that has the
#      previewed modification; the preview must come from the instance whose playback renderer has it
#   Checks 1-3, 5, 6, 6b, 6c and 6d run with the engine disabled (GLISS_ENGINE_DISABLED). With the real engine (python of the main
#   worktree's .venv, cwd = this worktree's engine, Praat for the pitch, a temporary work folder):
#   7. ARA SDK TestHost, all test cases
#   8. GlissARATest (plugin/tests/aratest): a test edit (GLISS_TEST_EDIT) -> render -> archive -> restore in another work
#      folder -> render, compared with the engine's render_region by plugin/tests/verify_ara_engine.py; restoring the
#      archive (in a new work folder, and again in the same one) does not tell the host that the document changed
#   9. GlissHostCheck --ara-editor: a real ARA document (JUCE ARA hosting), the editor bound to it, the page's engine calls
#   10. GlissARATest -relay: an external AI (another vocal_engine.mcp process, plugin/tests/relay_client.py) lists the open
#       documents, attaches to the modification and shifts it by +100 cents through the relay; the plug-in picks it up
#       (render, notes, archive), checked by plugin/tests/verify_ara_relay.py; the relay record is removed at the end
#   11. GlissARATest -changes: every change of what the archive holds tells the host (screen edit through the plug-in's
#       test bridge, lyrics only, F0 method only, guide, undo/redo, external edit and guide), selection alone does not,
#       and restoring the stored document (same and another work folder) tells nothing; an archive of the older renderer
#       with a pitch curve tells the host that the sound changed. Per-step counts in aratest-changes\changes.json
#   12. no engine process is left behind
# The engine checks are skipped (reported) when no engine python is found (-EnginePython, GLISS_ENGINE_PYTHON, or the
# .venv of the main worktree).
# Nothing is written outside plugin\build and the temp folder (the engine's work and log folders, the plug-in state file
# are redirected there); the plug-in is never installed.
param(
    [switch]$SkipBuild,
    [string]$Config = 'Release',
    [string]$Python = 'python',
    [string]$EnginePython = '',
    [int]$TimeoutSec = 180
)

$ErrorActionPreference = 'Stop'
$plugin = Split-Path -Parent $PSScriptRoot
$build = Join-Path $plugin 'build'
$deps = Join-Path $build '_deps'
# ARA SDK hosts (ARATestHost and GlissARATest, plugin/tests/aratest) in one tree
$exBuild = Join-Path $build 'ara-hosts'
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
# The hosts this run started. The leftover checks at the end look only at these and their children, so a run in
# another worktree at the same time is not counted (or killed).
$startedPids = New-Object 'System.Collections.Generic.List[int]'

function Invoke-Checked([string]$Exe, [string[]]$ExeArgs, [string]$Name, [hashtable]$Env = @{}) {
    # Run with a timeout; stdout+stderr go to $work\<Name>.log. Returns the exit code (-999 on timeout).
    $log = Join-Path $work ($Name + '.log')
    $saved = @{}
    foreach ($k in $Env.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, $Env[$k]) }
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList $ExeArgs -PassThru -NoNewWindow `
            -RedirectStandardOutput $log -RedirectStandardError ($log + '.err')
        $null = $p.Handle   # keep the handle so ExitCode is readable after exit
        $startedPids.Add($p.Id)
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
        # GLISS_TEST_HOOKS: the test bridge (GLISS_TEST_BRIDGE_DIR) used by GlissARATest -changes; release builds leave it out
        cmake -S $plugin -B $build -G 'Visual Studio 17 2022' -A x64 -Wno-dev -DGLISS_TEST_HOOKS=ON | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'cmake configure (plugin) failed' }
        cmake --build $build --config $Config --target GlissARA_VST3 GlissHostCheck GlissPluginTests --parallel 8 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'cmake build (plugin) failed' }

        # ARA SDK examples (TestHost). They need the VST3 SDK that the ARA SDK installs with its own script.
        if (-not (Test-Path (Join-Path $vst3Sdk 'cmake'))) {
            cmake "-DVST3_SDK_DIR=$vst3Sdk" -P (Join-Path $araSdk 'install_vst3sdk.cmake') | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'installing the VST3 SDK for the ARA examples failed' }
        }
        cmake -S (Join-Path $plugin 'tests\aratest') -B $exBuild -G 'Visual Studio 17 2022' -A x64 -Wno-dev `
            "-DGLISS_ARA_SDK_DIR=$araSdk" "-DARA_VST3_SDK_DIR=$vst3Sdk" -DARA_SETUP_DEBUGGING=OFF -DSMTG_CREATE_PLUGIN_LINK=OFF | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'cmake configure (ARA hosts) failed' }
        cmake --build $exBuild --config $Config --target ARATestHost GlissARATest --parallel 8 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'cmake build (ARA TestHost, GlissARATest) failed' }
    }

    $testHost = Join-Path $exBuild "bin\$Config\ARATestHost.exe"
    $gliss = Join-Path $build "GlissARA_artefacts\$Config\VST3\Gliss.vst3\Contents\x86_64-win\Gliss.vst3"
    $hostCheck = Join-Path $build "tests\hostcheck\GlissHostCheck_artefacts\$Config\GlissHostCheck.exe"
    $unitTests = Join-Path $build "tests\unit\GlissPluginTests_artefacts\$Config\GlissPluginTests.exe"
    $araTest = Join-Path $exBuild "bin\$Config\GlissARATest.exe"
    foreach ($f in @($testHost, $gliss, $hostCheck, $unitTests, $araTest)) { if (-not (Test-Path $f)) { throw "missing: $f" } }

    # Keep the engine and the plug-in away from the user's folders (%LOCALAPPDATA%\Gliss, %APPDATA%\Gliss, ~\.vocal-editor)
    foreach ($kv in @{ VOCAL_ENGINE_WORK_DIR = (Join-Path $work 'engine-work'); VOCAL_ENGINE_LOG_DIR = (Join-Path $work 'engine-logs');
                       GLISS_PLUGIN_STATE_FILE = (Join-Path $work 'plugin-state.json'); GLISS_F0_ESTIMATOR = 'praat';
                       VOCAL_ENGINE_AUTO_LYRICS = '0' }.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($kv.Key, $kv.Value)
    }
    $noEngine = @{ GLISS_ENGINE_DISABLED = '1' }

    # The engine: python of the main worktree's .venv (worktrees have none), cwd = this worktree's engine
    $repo = Split-Path -Parent $plugin
    if (-not $EnginePython) { $EnginePython = $env:GLISS_ENGINE_PYTHON }
    if (-not $EnginePython) {
        $mainTree = (& git -C $repo worktree list --porcelain | Select-Object -First 1) -replace '^worktree ', ''
        foreach ($root in @($repo, $mainTree)) {
            $candidate = Join-Path $root '.venv\Scripts\python.exe'
            if ($root -and (Test-Path $candidate)) { $EnginePython = $candidate; break }
        }
    }
    $engineEnv = @{ GLISS_ENGINE_PYTHON = $EnginePython; GLISS_ENGINE_CWD = (Join-Path $repo 'engine') }
    $enginesBefore = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'vocal_engine\.mcp' } | ForEach-Object { $_.ProcessId })

    # 1. TestHost, all test cases
    $code = Invoke-Checked $testHost @('-vst3', $gliss) 'testhost-all' $noEngine
    $log = Get-Content (Join-Path $work 'testhost-all.log.err'), (Join-Path $work 'testhost-all.log') -ErrorAction SilentlyContinue
    $ran = ($log | Select-String -SimpleMatch '*** testing').Count
    Report 'ARA TestHost (all test cases)' ($code -eq 0 -and $ran -ge 12) "exit=$code testCases=$ran"

    # 2. TestHost, PlaybackRendering, trace compared with the SDK test signal.
    #    TestHost renders at CPU speed in "realtime" mode, so wait for the prefetch (GLISS_ARA_READ_TIMEOUT_MS)
    #    to get complete blocks to compare. Without it, unfinished reads are zero-filled and skipped by the checker.
    $trace = Join-Path $work 'trace'
    New-Item -ItemType Directory -Force $trace | Out-Null
    $code = Invoke-Checked $testHost @('-vst3', $gliss, '-test', 'PlaybackRendering') 'testhost-render' `
        @{ GLISS_ARA_TRACE_DIR = $trace; GLISS_ARA_READ_TIMEOUT_MS = '3000'; GLISS_ENGINE_DISABLED = '1' }
    $verify = & $Python (Join-Path $plugin 'tests\verify_render_trace.py') $trace
    Report 'ARA TestHost (PlaybackRendering, trace vs. SDK signal)' ($code -eq 0 -and $LASTEXITCODE -eq 0) "exit=$code $verify"

    # 3. JUCE host check
    $hcTrace = Join-Path $work 'hostcheck-trace'
    New-Item -ItemType Directory -Force $hcTrace | Out-Null
    $report = Join-Path $work 'hostcheck-report.txt'
    $code = Invoke-Checked $hostCheck @($report, $gliss, $hcTrace) 'hostcheck' @{ GLISS_ARA_TRACE_DIR = $hcTrace; GLISS_ENGINE_DISABLED = '1' }
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

    # 6b. the renderer keeps playing after the host changes things without prepareToPlay (plugin.log is redirected here)
    $report = Join-Path $work 'ara-playback-report.txt'
    $playbackLog = Join-Path $work 'ara-playback-plugin.log'
    [System.IO.File]::Delete($playbackLog)
    $code = Invoke-Checked $hostCheck @('--ara-playback', $report, $gliss, $playbackLog) 'ara-playback' `
        @{ GLISS_ENGINE_DISABLED = '1'; GLISS_PLUGIN_LOG_FILE = $playbackLog }
    $summary = (Get-Content $report -ErrorAction SilentlyContinue | Select-String 'RESULT').Line
    Report 'GlissHostCheck --ara-playback (readers and regions after prepare)' ($code -eq 0) "exit=$code $summary"
    if ($code -ne 0 -and (Test-Path $report)) { Get-Content $report | Write-Host }

    # 6c. two plug-in instances on one document: only the instance that asked for a preview adds it (test bridge "@preview")
    $previewBridge = Join-Path $work 'ara-preview-bridge'
    $previewTrace = Join-Path $work 'ara-preview-trace'
    foreach ($d in @($previewBridge, $previewTrace)) {
        if (Test-Path $d) { [System.IO.Directory]::Delete($d, $true) }
        New-Item -ItemType Directory -Force $d | Out-Null
    }
    $report = Join-Path $work 'ara-preview-report.txt'
    $code = Invoke-Checked $hostCheck @('--ara-preview', $report, $gliss, $previewBridge, $previewTrace) 'ara-preview' `
        @{ GLISS_ENGINE_DISABLED = '1'; GLISS_TEST_BRIDGE_DIR = $previewBridge; GLISS_ARA_TRACE_DIR = $previewTrace
           GLISS_PLUGIN_LOG_FILE = (Join-Path $work 'ara-preview-plugin.log') }
    $summary = (Get-Content $report -ErrorAction SilentlyContinue | Select-String 'RESULT').Line
    Report 'GlissHostCheck --ara-preview (the preview is added by the instance that asked)' ($code -eq 0) "exit=$code $summary"
    if ($code -ne 0 -and (Test-Path $report)) { Get-Content $report | Write-Host }

    # 6d. only the playback renderers have regions (the editor renderers have none)
    $playbackBridge = Join-Path $work 'ara-preview-playback-bridge'
    $playbackTrace = Join-Path $work 'ara-preview-playback-trace'
    foreach ($d in @($playbackBridge, $playbackTrace)) {
        if (Test-Path $d) { [System.IO.Directory]::Delete($d, $true) }
        New-Item -ItemType Directory -Force $d | Out-Null
    }
    $report = Join-Path $work 'ara-preview-playback-report.txt'
    $code = Invoke-Checked $hostCheck @('--ara-preview-playback', $report, $gliss, $playbackBridge, $playbackTrace) 'ara-preview-playback' `
        @{ GLISS_ENGINE_DISABLED = '1'; GLISS_TEST_BRIDGE_DIR = $playbackBridge; GLISS_ARA_TRACE_DIR = $playbackTrace
           GLISS_PLUGIN_LOG_FILE = (Join-Path $work 'ara-preview-playback-plugin.log') }
    $summary = (Get-Content $report -ErrorAction SilentlyContinue | Select-String 'RESULT').Line
    Report 'GlissHostCheck --ara-preview-playback (the preview comes from the instance that plays the modification)' ($code -eq 0) "exit=$code $summary"
    if ($code -ne 0 -and (Test-Path $report)) { Get-Content $report | Write-Host }

    if (-not $EnginePython -or -not (Test-Path $EnginePython)) {
        Write-Host "SKIP engine checks 7-11: no engine python (pass -EnginePython or set GLISS_ENGINE_PYTHON)"
    } else {
        Write-Host "engine: $EnginePython (cwd $($engineEnv.GLISS_ENGINE_CWD))"

        # 7. TestHost, all test cases, with the engine
        $env7 = $engineEnv.Clone(); $env7.GLISS_ARA_TRACE_DIR = (Join-Path $work 'trace-engine-all')
        New-Item -ItemType Directory -Force $env7.GLISS_ARA_TRACE_DIR | Out-Null
        $code = Invoke-Checked $testHost @('-vst3', $gliss) 'testhost-all-engine' $env7
        $log = Get-Content (Join-Path $work 'testhost-all-engine.log.err'), (Join-Path $work 'testhost-all-engine.log') -ErrorAction SilentlyContinue
        $ran = ($log | Select-String -SimpleMatch '*** testing').Count
        Report 'ARA TestHost (all test cases, with the engine)' ($code -eq 0 -and $ran -ge 12) "exit=$code testCases=$ran"

        # 8. edit -> render -> archive -> restore elsewhere -> render, compared with the engine's render_region
        $out = Join-Path $work 'aratest'
        $workA = Join-Path $work 'aratest-work-a'
        $workB = Join-Path $work 'aratest-work-b'
        foreach ($d in @($out, $workA, $workB)) { New-Item -ItemType Directory -Force $d | Out-Null }
        $env8 = $engineEnv.Clone()
        $env8.VOCAL_ENGINE_WORK_DIR = $workA
        $env8.GLISS_TEST_EDIT = '{"cents": 100, "start_sec": 0, "end_sec": 6.2}'
        $env8.GLISS_ARA_SYNC_WAIT_MS = '120000'
        $env8.GLISS_ARA_READ_TIMEOUT_MS = '3000'
        $env8.GLISS_ARA_TRACE_DIR = (Join-Path $work 'trace-aratest')
        New-Item -ItemType Directory -Force $env8.GLISS_ARA_TRACE_DIR | Out-Null
        $code = Invoke-Checked $araTest @('-vst3', $gliss, '-out', $out, '-workB', $workB) 'aratest' $env8
        Report 'GlissARATest (edit, render, archive, restore, render)' ($code -eq 0) "exit=$code"
        if ($code -eq 0) {
            Push-Location $engineEnv.GLISS_ENGINE_CWD
            try {
                $verify = & $EnginePython (Join-Path $plugin 'tests\verify_ara_engine.py') $out $workA $workB 2>&1
                $verifyCode = $LASTEXITCODE
            } finally { Pop-Location }
            $verify | Where-Object { $_ -match '^(PASS|FAIL|RESULT)' } | ForEach-Object { Write-Host "    $_" }
            Report 'render == engine render_region, archive round trip (verify_ara_engine.py)' ($verifyCode -eq 0) "exit=$verifyCode"
        }

        # 9. a real ARA document with the editor bound to it
        $env9 = $engineEnv.Clone()
        $env9.VOCAL_ENGINE_WORK_DIR = (Join-Path $work 'hostcheck-ara-work')
        $env9.GLISS_ARA_TRACE_DIR = (Join-Path $work 'trace-ara-editor')
        New-Item -ItemType Directory -Force $env9.GLISS_ARA_TRACE_DIR | Out-Null
        $report = Join-Path $work 'ara-editor-report.txt'
        $code = Invoke-Checked $hostCheck @('--ara-editor', $report, $gliss, $env9.GLISS_ARA_TRACE_DIR, '--timeout', '150') 'ara-editor' $env9
        $summary = (Get-Content $report -ErrorAction SilentlyContinue | Select-String 'RESULT').Line
        Report 'GlissHostCheck --ara-editor (real document and engine)' ($code -eq 0) "exit=$code $summary"
        if ($code -ne 0 -and (Test-Path $report)) { Get-Content $report | Write-Host }

        # 10. an external AI edits the open document through the relay (engine/vocal_engine/ara_relay.py)
        $outR = Join-Path $work 'aratest-relay'
        $workR = Join-Path $work 'aratest-relay-work'
        $traceR = Join-Path $work 'trace-relay'
        $sessR = Join-Path $work 'relay-sessions'
        foreach ($d in @($outR, $workR, $traceR, $sessR)) { New-Item -ItemType Directory -Force $d | Out-Null }
        $env10 = $engineEnv.Clone()
        $env10.VOCAL_ENGINE_WORK_DIR = $workR
        $env10.GLISS_ARA_SESSIONS_DIR = $sessR
        $env10.GLISS_ARA_SYNC_WAIT_MS = '120000'
        $env10.GLISS_ARA_READ_TIMEOUT_MS = '3000'
        $env10.GLISS_ARA_TRACE_DIR = $traceR
        $code = Invoke-Checked $araTest @('-vst3', $gliss, '-out', $outR, '-relay', (Join-Path $plugin 'tests\relay_client.py')) 'aratest-relay' $env10
        Report 'GlissARATest -relay (external AI: list, attach, edit)' ($code -eq 0) "exit=$code"
        if ($code -eq 0) {
            Push-Location $engineEnv.GLISS_ENGINE_CWD
            try {
                $verify = & $EnginePython (Join-Path $plugin 'tests\verify_ara_relay.py') $outR $workR $traceR 2>&1
                $verifyCode = $LASTEXITCODE
            } finally { Pop-Location }
            $verify | Where-Object { $_ -match '^(PASS|FAIL|RESULT)' } | ForEach-Object { Write-Host "    $_" }
            Report 'external edit is played, saved and noticed (verify_ara_relay.py)' ($verifyCode -eq 0) "exit=$verifyCode"
        } elseif (Test-Path (Join-Path $outR 'relay-client.json')) {
            Get-Content (Join-Path $outR 'relay-client.json') | Write-Host
        }
        $records = @(Get-ChildItem $sessR -Filter '*.json' -ErrorAction SilentlyContinue)
        Report 'relay records removed when the document closed' ($records.Count -eq 0) ("left=" + $records.Count)

        # 11. host notifications for every change of the saved state, none for restoring only
        $outX = Join-Path $work 'aratest-changes'
        $workX = Join-Path $work 'aratest-changes-work'
        $workX2 = Join-Path $work 'aratest-changes-work-2'
        $sessX = Join-Path $work 'changes-sessions'
        foreach ($d in @($outX, $workX, $workX2, $sessX)) { New-Item -ItemType Directory -Force $d | Out-Null }
        $env11 = $engineEnv.Clone()
        $env11.VOCAL_ENGINE_WORK_DIR = $workX
        $env11.GLISS_ARA_SESSIONS_DIR = $sessX
        $env11.GLISS_ARA_SYNC_WAIT_MS = '120000'
        $env11.GLISS_ARA_READ_TIMEOUT_MS = '3000'
        $env11.GLISS_ARA_TRACE_DIR = (Join-Path $work 'trace-changes')
        New-Item -ItemType Directory -Force $env11.GLISS_ARA_TRACE_DIR | Out-Null
        $saved = $TimeoutSec
        $TimeoutSec = [Math]::Max($TimeoutSec, 900)
        try {
            $code = Invoke-Checked $araTest @('-vst3', $gliss, '-out', $outX, '-changes', (Join-Path $plugin 'tests\relay_client.py'), '-workB', $workX2) 'aratest-changes' $env11
        } finally { $TimeoutSec = $saved }
        $steps = Get-Content (Join-Path $work 'aratest-changes.log.err'), (Join-Path $work 'aratest-changes.log') -ErrorAction SilentlyContinue |
            Select-String 'changes: \d+ '
        $steps | ForEach-Object { Write-Host ("    " + ($_.Line -replace '^.*changes: ', '')) }
        Report 'GlissARATest -changes (every saved-state change tells the host, restoring does not)' ($code -eq 0) "exit=$code steps=$($steps.Count)"
    }
}
finally {
    # nothing we started may remain (only our hosts: another worktree may be running this script at the same time)
    $left = @(Get-Process -Name ARATestHost, GlissARATest, GlissHostCheck, GlissPluginTests -ErrorAction SilentlyContinue |
        Where-Object { $startedPids.Contains($_.Id) })
    foreach ($p in $left) { & taskkill /T /F /PID $p.Id | Out-Null }
    Report 'no leftover test processes' ($left.Count -eq 0) ("killed=" + $left.Count)

    # WebView2 browser processes of the editor (user data folder %TEMP%\GlissARA-<pid of our host>) end shortly after the host quits
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $webviews = @(Get-CimInstance Win32_Process -Filter "Name = 'msedgewebview2.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'GlissARA-(\d+)' -and $startedPids.Contains([int]$Matches[1]) })
        if ($webviews.Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    foreach ($w in $webviews) { & taskkill /T /F /PID $w.ProcessId | Out-Null }
    Report 'no leftover WebView2 processes' ($webviews.Count -eq 0) ("killed=" + $webviews.Count)

    # Engines started by the plug-in run in a Job Object of the host process and end with it. Engines that other
    # programs started (an MCP client, the app, a run in another worktree) are left alone: only new ones whose parent is
    # one of our hosts count (the parent's ID stays on the process after the host has quit).
    if ($null -ne $enginesBefore) {
        $deadline = (Get-Date).AddSeconds(10)
        do {
            $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
            $new = @($all | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'vocal_engine\.mcp' -and $enginesBefore -notcontains $_.ProcessId })
            # ours: the parent is one of our hosts; plus the children of those (the .venv launcher starts the real python)
            $ours = @($new | Where-Object { $startedPids.Contains([int]$_.ParentProcessId) })
            $oursIds = @($ours | ForEach-Object { [int]$_.ProcessId })
            $engines = @($ours) + @($new | Where-Object { $oursIds -contains [int]$_.ParentProcessId })
            if ($engines.Count -eq 0) { break }
            Start-Sleep -Milliseconds 500
        } while ((Get-Date) -lt $deadline)
        foreach ($e in $engines) { & taskkill /T /F /PID $e.ProcessId | Out-Null }
        Report 'no leftover engine processes' ($engines.Count -eq 0) ("killed=" + $engines.Count)
    }
    Write-Host "logs: $work"
}

if ($failed.Count -gt 0) { Write-Host ("FAILED: " + ($failed -join ', ')); exit 1 }
Write-Host 'ALL PASSED'
exit 0
