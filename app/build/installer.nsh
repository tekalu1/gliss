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

; ---- DAW plug-in (VST3 + ARA 2). Notes in Japanese: docs/ara-plugin.md ("配布") ----
; The installation carries resources\plugin\Gliss.vst3 (electron-builder.yml extraResources). The installer copies it to
; the per-user VST3 folder ($LOCALAPPDATA\Programs\Common\VST3, from the VST3 spec; no admin rights) and writes
; gliss-install.json into Contents\Resources of both copies, so that the plug-in finds the engine of this installation
; (resources\engine\vocal-engine\vocal-engine.exe, also when the install folder was changed; plugin/src/engine/EngineConfig.cpp).
; A DAW that has the plug-in loaded keeps Gliss.vst3 (the DLL) locked: it cannot be deleted or overwritten, but it can be
; renamed. The installer renames it to Gliss.vst3.<tick>.old and puts the new one beside it; the DAW keeps the old version
; until it restarts, and the next install deletes the .old file.
!include "WordFunc.nsh"
!define GLISS_VST3_DIR "$LOCALAPPDATA\Programs\Common\VST3"

; Remove a folder; when it is a link (junction / symlink), remove only the link, not what it points to.
!macro glissRemoveDir DIR
  System::Call 'kernel32::GetFileAttributesW(t "${DIR}") i.R4'
  ${if} $R4 != -1
    IntOp $R4 $R4 & 0x400
    ${if} $R4 != 0
      RMDir "${DIR}"
    ${else}
      RMDir /r "${DIR}"
    ${endIf}
  ${endIf}
!macroend

; <bundle>\Contents\Resources\gliss-install.json: UTF-16LE with a BOM (NSIS cannot write UTF-8; the plug-in reads both).
!macro glissWriteInstallJson BUNDLE
  CreateDirectory "${BUNDLE}\Contents\Resources"
  ${WordReplace} "$INSTDIR" "\" "\\" "+" $R8
  FileOpen $R7 "${BUNDLE}\Contents\Resources\gliss-install.json" w
  FileWriteUTF16LE /BOM $R7 '{"format":"gliss-install","version":1,"installDir":"$R8","appVersion":"${VERSION}"}$\r$\n'
  FileClose $R7
!macroend

!macro glissInstallPlugin VST3DIR
  Push $R4
  Push $R5
  Push $R6
  Push $R7
  Push $R8
  Push $R9
  StrCpy $R9 "${VST3DIR}\Gliss.vst3"
  StrCpy $R6 "$R9\Contents\x86_64-win"
  !insertmacro glissWriteInstallJson "$INSTDIR\resources\plugin\Gliss.vst3"
  ; the previous version, and the .old DLLs of earlier updates that are no longer loaded
  !insertmacro glissRemoveDir "$R9"
  ${if} ${FileExists} "$R6\Gliss.vst3"
    System::Call 'kernel32::GetTickCount() i.R5'
    Rename "$R6\Gliss.vst3" "$R6\Gliss.vst3.$R5.old"
  ${endIf}
  ${if} ${FileExists} "$R6\Gliss.vst3"
    DetailPrint "Gliss.vst3 is in use and could not be replaced: $R9"
  ${else}
    CreateDirectory "${VST3DIR}"
    CopyFiles /SILENT "$INSTDIR\resources\plugin\Gliss.vst3" "${VST3DIR}"
  ${endIf}
  ${if} ${FileExists} "$R6\Gliss.vst3"
  ${andIf} ${FileExists} "$R9\Contents\Resources\gliss-install.json"
    ${if} ${FileExists} "$R6\Gliss.vst3.*.old"
    ${andIfNot} ${Silent}
      MessageBox MB_OK|MB_ICONINFORMATION "DAW が Gliss のプラグインを使っています。DAW を起動し直すと、新しい版のプラグインになります。"
    ${endIf}
  ${else}
    DetailPrint "Could not install the DAW plug-in to $R9"
    ${ifNot} ${Silent}
      MessageBox MB_OK|MB_ICONEXCLAMATION "DAW のプラグイン（Gliss.vst3）を $R9 に入れられませんでした。Gliss の画面はこのまま使えます。DAW で使うときは、DAW を閉じてからもう一度インストールしてください。"
    ${endIf}
  ${endIf}
  Pop $R9
  Pop $R8
  Pop $R7
  Pop $R6
  Pop $R5
  Pop $R4
!macroend

; Only the copy that the installer made (it has gliss-install.json). A DAW that has it loaded blocks the removal:
; ask to close the DAW and retry (silent: leave it; the plug-in then reports that the engine is missing).
!macro glissRemovePlugin VST3DIR
  Push $R0
  Push $R4
  Push $R9
  StrCpy $R9 "${VST3DIR}\Gliss.vst3"
  ${if} ${FileExists} "$R9\Contents\Resources\gliss-install.json"
    ${do}
      !insertmacro glissRemoveDir "$R9"
      ${ifNot} ${FileExists} "$R9\*.*"
      ${orIf} ${Silent}
        ${break}
      ${endIf}
      ; MB_RETRYCANCEL | MB_ICONEXCLAMATION; IDRETRY = 4
      System::Call 'user32::MessageBoxW(p $HWNDPARENT, t "DAW が Gliss のプラグインを使っているため、$R9 を消せません。DAW を閉じてから「再試行」を押してください。「キャンセル」で、このフォルダを残して進めます（後で手で消せます）。", t "Gliss", i 0x35) i.R0'
    ${loopUntil} $R0 != 4
  ${endIf}
  Pop $R9
  Pop $R4
  Pop $R0
!macroend

!macro customInstall
  !insertmacro glissInstallPlugin "${GLISS_VST3_DIR}"
!macroend

; Uninstall: also delete the auto-update cache (%LOCALAPPDATA%\gliss-updater: installer.exe = a copy of the installer that
; electron-builder keeps for differential updates, and pending\ = the downloaded update; about 440 MB). The uninstaller leaves it.
; Only when the user really uninstalls: a newer installer that replaces this version calls the old uninstaller with
; --updated (both for an in-app update and for a manual install over an existing one), and in the in-app update the running
; installer itself lives in gliss-updater\pending\. Keep this directory name equal to updaterCacheDirName (package name + "-updater").
; The DAW plug-in in the VST3 folder is removed at the same time (an update replaces it in customInstall instead).
!macro customUnInstall
  ${ifNot} ${isUpdated}
    RMDir /r "$LOCALAPPDATA\gliss-updater"
    !insertmacro glissRemovePlugin "${GLISS_VST3_DIR}"
  ${endIf}
!macroend
