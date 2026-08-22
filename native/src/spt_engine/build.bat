@echo off
rem Build the SpeedTree ground-truth harness.
rem
rem 32-BIT on purpose: Oblivion.exe is i386 with its relocations STRIPPED, so
rem it can only be mapped at its fixed image base 0x400000, and only a 32-bit
rem host can address that.
rem
rem The built .exe is COMMITTED to native/dist/ (see that README): the
rem conversion needs it and most machines running this have no C++ compiler.
rem Invoked by `python native/build.py --programs`, or directly.
setlocal enabledelayedexpansion

rem Locate MSVC through vswhere rather than a hardcoded version directory --
rem the previous hardcoded "Visual Studio\18\BuildTools" path only existed on
rem one machine.  Build Tools are enough; a full Visual Studio is not required.
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VCVARS="
if exist "%VSWHERE%" (
  for /f "usebackq delims=" %%I in (`"%VSWHERE%" -all -products * -property installationPath`) do (
    if exist "%%I\VC\Auxiliary\Build\vcvarsall.bat" set "VCVARS=%%I\VC\Auxiliary\Build\vcvarsall.bat"
  )
)
if not defined VCVARS (
  echo ERROR: MSVC not found via vswhere.
  echo Install "Build Tools for Visual Studio" with the C++ workload,
  echo including the x86 target.
  exit /b 1
)

rem vcvarsall.bat shells out to vswhere.exe by BARE NAME, so the VS Installer
rem directory must be on PATH or it prints "not recognized" and can leave the
rem environment half-initialised (build.py hits the same thing).
set "PATH=%PATH%;%ProgramFiles(x86)%\Microsoft Visual Studio\Installer"
call "%VCVARS%" x86 >nul || exit /b 1

rem Source lives here; the artifact goes to native/dist/ alongside the
rem committed .pyd.  %~dp0 is native/src/spt_engine/.
set "SRC=%~dp0spt_engine_dump.cpp"
set "OUTDIR=%~dp0..\..\dist"
set "OBJDIR=%~dp0..\..\build\spt_engine"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"
if not exist "%OBJDIR%" mkdir "%OBJDIR%"

rem /BASE:0x20000000 keeps the HOST out of 0x400000, which the mapped
rem              Oblivion.exe must occupy (its relocations are stripped).
rem /DYNAMICBASE:NO stops ASLR from moving the host back on top of it.
rem /GS-  : the host's own stack-guard fires when engine code returns through
rem         our frames; its __report_gsfailure calls ExitProcess(0xC000000D),
rem         which reads exactly like an engine crash.  We call foreign code by
rem         design, so the guard is noise here.
rem /EHa  : engine functions raise SEH; __try must be able to catch it.
cl /nologo /O2 /EHa /GS- /std:c++17 "%SRC%" /Fo"%OBJDIR%\\" ^
   /Fe:"%OUTDIR%\spt_engine_dump.exe" ^
   /link /BASE:0x20000000 /FIXED /DYNAMICBASE:NO
set RC=%ERRORLEVEL%
if not "%RC%"=="0" exit /b %RC%
echo built %OUTDIR%\spt_engine_dump.exe
