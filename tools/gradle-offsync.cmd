@echo off
rem Run Gradle with build output OFF the OneDrive-synced tree (see CLAUDE.md).
rem OneDrive locks build/ mid-compile, so redirect buildDirectory + the project
rem cache under %TEMP%. The init script is regenerated when missing or EMPTY --
rem an empty one silently does nothing, which puts the build back inside the
rem synced tree and reintroduces the "Access is denied" / "Unable to delete"
rem failures this file exists to avoid.
rem
rem Usage (from anywhere -- paths resolve from this script's own location):
rem   tools\gradle-offsync.cmd assembleRelease
rem   tools\gradle-offsync.cmd :app:testDebugUnitTest
rem The APK lands under %TEMP%\lazeR-build\app\outputs\apk\release\, NOT
rem android\app\build\.
setlocal
set "ANDROID_DIR=%~dp0..\android"
set "INIT=%TEMP%\lazeR-init.gradle"
rem Forward slashes on purpose: this string is a Groovy literal, where a lone
rem backslash starts an escape ("\U", "\b" ...) and fails the build. java.io.File
rem accepts "/" on Windows.
set "OUT=%TEMP:\=/%/lazeR-build"

set "NEEDS_INIT=1"
if exist "%INIT%" for %%F in ("%INIT%") do if %%~zF GTR 0 set "NEEDS_INIT=0"
if "%NEEDS_INIT%"=="1" (
    echo [gradle] writing "%INIT%"
    > "%INIT%" echo allprojects {
    >>"%INIT%" echo     layout.buildDirectory.set^(new File^("%OUT%", project.name^)^)
    >>"%INIT%" echo }
)

"%ANDROID_DIR%\gradlew.bat" -p "%ANDROID_DIR%" %* --no-daemon --init-script "%INIT%" --project-cache-dir "%TEMP%\lazeR-gradle-cache"
endlocal & exit /b %ERRORLEVEL%
