@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "MAKEFILE=%CD%\makefile"
if not exist "!MAKEFILE!" set "MAKEFILE="
if not defined MAKEFILE for /r "%~dp0.." %%F in (makefile) do if not defined MAKEFILE set "MAKEFILE=%%F"
if not defined MAKEFILE (
    echo Unable to locate a generated makefile. 1>&2
    exit /b 1
)
set "COMPILER="
for /f "tokens=1" %%P in ('findstr /i "tiarmclang.exe" "!MAKEFILE!"') do if not defined COMPILER set "COMPILER=%%~P"
for /f "tokens=3" %%P in ('findstr /b "CG_TOOL_ROOT" "!MAKEFILE!"') do if not defined COMPILER set "COMPILER=%%P\bin\tiarmclang.exe"
if not defined COMPILER (
    echo Unable to locate tiarmclang.exe in the generated makefile. 1>&2
    exit /b 1
)
set "HAS_COMPILER_LIB="
for %%A in (%*) do echo %%~A| findstr /i /c:"tools/compiler" >nul && set "HAS_COMPILER_LIB=1"
if defined HAS_COMPILER_LIB (
    set "COMPILER_BIN=!COMPILER:tiarmclang.exe=!"
    call "%COMPILER%" %* -Wl,-i"!COMPILER_BIN!..\lib"
) else (
    call "%COMPILER%" %*
)
exit /b %errorlevel%
