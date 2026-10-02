; Custom NSIS include for the Gliss installer / uninstaller (electron-builder picks up build/installer.nsh).
; Notes in Japanese: electron-builder.yml (nsis section) and docs/release-plan.md.
;
; Why: electron-builder 26.15.x's default CHECK_APP_RUNNING looks for processes under $INSTDIR with
;   (Get-CimInstance Win32_Process | ? { $_.Path.StartsWith('$INSTDIR') }).Count -gt 0
; In Windows PowerShell 5.1 a single CimInstance has no .Count (it is $null), so when EXACTLY ONE process
; runs from the install dir (typically one AI client's vocal-engine.exe) it is reported as "not running".
; The installer then goes on, and the old uninstaller (which renames every file out of the install dir
; and rolls back when one is locked) fails with exit code 2; the user only sees "Gliss cannot be closed".
;
; Fix: before the default check, stop every process that runs from $INSTDIR\resources\engine\ (the engine
; that Claude Code / Claude Desktop started). The Gliss window itself still goes through the default check
; (it always has several processes, so it is found and closed as before).
!include "getProcessInfo.nsh"
Var pid

!macro customCheckAppRunning
  !insertmacro IS_POWERSHELL_AVAILABLE
  ${if} $IsPowerShellAvailable == 0
    nsExec::Exec `"$PowerShellPath" -C "Get-CimInstance -ClassName Win32_Process | ? {$$_.ExecutablePath -and $$_.ExecutablePath.StartsWith('$INSTDIR\resources\engine\', 'CurrentCultureIgnoreCase')} | % { Stop-Process -Id $$_.ProcessId -Force }"`
    Pop $0
    # let Windows release the file handles of the stopped engines
    Sleep 500
  ${endIf}
  !insertmacro _CHECK_APP_RUNNING
!macroend

; Uninstall: also delete the auto-update cache (%LOCALAPPDATA%\gliss-updater: installer.exe = a copy of the installer that
; electron-builder keeps for differential updates, and pending\ = the downloaded update; about 440 MB). The uninstaller leaves it.
; Only when the user really uninstalls: a newer installer that replaces this version calls the old uninstaller with
; --updated (both for an in-app update and for a manual install over an existing one), and in the in-app update the running
; installer itself lives in gliss-updater\pending\. Keep this directory name equal to updaterCacheDirName (package name + "-updater").
!macro customUnInstall
  ${ifNot} ${isUpdated}
    RMDir /r "$LOCALAPPDATA\gliss-updater"
  ${endIf}
!macroend
