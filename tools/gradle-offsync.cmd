@echo off
rem Run Gradle with build output OFF the OneDrive-synced tree (see CLAUDE.md).
rem OneDrive locks build/ mid-compile, so redirect buildDirectory + the project
rem cache under %TEMP%. The init script is rewritten on every run: a stale one
rem (an old path) or an empty one (which parses fine and does nothing, putting
rem the build back inside the synced tree) would otherwise be kept forever.
rem
rem Usage (from anywhere -- paths resolve from this script's own location):
rem   tools\gradle-offsync.cmd assembleRelease
rem   tools\gradle-offsync.cmd :app:testDebugUnitTest
rem The APK lands under %TEMP%\lazeR-build\app\outputs\apk\release\, NOT
rem android\app\build\.
setlocal
set "ANDROID_DIR=%~dp0..\android"

rem LONG form of %TEMP%, never the raw value. With a profile name over 8
rem characters Windows sets TEMP to the 8.3 short form (C:\Users\BISMAR~1\...),
rem and Gradle's first use of a project cache there dies with "Cannot delete
rem file: ...\buildOutputCleanup.lock" -- it compares the short and long
rem spellings of its own lock file, misses, and tries to delete the lock it holds.
rem See publish_release.ps1. cmd's %%~f doesn't expand 8.3 names; Get-Item does.
set "TMPDIR="
for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "(Get-Item -LiteralPath $env:TEMP).FullName"`) do set "TMPDIR=%%T"
if not defined TMPDIR set "TMPDIR=%TEMP%"

set "INIT=%TMPDIR%\lazeR-init.gradle"
rem Forward slashes on purpose: this string is a Groovy literal, where a lone
rem backslash starts an escape ("\U", "\b" ...) and fails the build. java.io.File
rem accepts "/" on Windows.
set "OUT=%TMPDIR:\=/%/lazeR-build"

> "%INIT%" echo allprojects {
>>"%INIT%" echo     layout.buildDirectory.set^(new File^("%OUT%", project.name^)^)
>>"%INIT%" echo }

"%ANDROID_DIR%\gradlew.bat" -p "%ANDROID_DIR%" %* --no-daemon --init-script "%INIT%" --project-cache-dir "%TMPDIR%\lazeR-gradle-cache"
endlocal & exit /b %ERRORLEVEL%
