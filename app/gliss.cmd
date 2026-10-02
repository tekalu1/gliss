@echo off
rem Open Gliss with one WAV as a new project's take (for DAW "external editor" settings, e.g. Reaper).
rem   gliss.cmd "D:\rec\take.wav"
rem Without an argument it reopens the last project. Run "pnpm install" in app\ once beforehand.
set "EXE=%~dp0node_modules\electron\dist\electron.exe"
if not exist "%EXE%" (
  echo Electron is not installed. Run "pnpm install" in %~dp0 first.
  pause
  exit /b 1
)
if "%~1"=="" (
  start "" "%EXE%" "%~dp0."
) else (
  start "" "%EXE%" "%~dp0." --take "%~f1"
)
