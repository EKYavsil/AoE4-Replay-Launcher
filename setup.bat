@echo off
setlocal
cd /d "%~dp0"
title AoE4 Replay Launcher - Setup

echo.
echo  AoE4 Replay Launcher - one-time setup
echo  =====================================
echo.
echo  Looking for Python 3.12 or newer...

call :detect_python
if defined PYEXE goto :have_python

echo  Python 3.12+ was not found on this PC.
echo.

rem Offer to install it with winget (built into Windows 10/11).
winget --version >nul 2>&1
if errorlevel 1 goto :no_winget

set "ANS="
set /p "ANS=Install Python 3.13 now using winget? [Y/N] "
if /i not "%ANS%"=="Y" goto :manual_python

echo.
echo  Installing Python 3.13 via winget...
echo  ^(If Windows shows a permission/UAC prompt, choose Yes - otherwise the
echo   install cannot complete.^)
winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :winget_failed

call :detect_python
if defined PYEXE goto :have_python

echo.
echo  Python was installed, but this window cannot see it yet ^(PATH not refreshed^).
echo  Please CLOSE this window and double-click setup.bat again.
echo.
pause
exit /b 0

:winget_failed
echo.
echo  Python installation did not complete. This usually means a Windows
echo  permission ^(UAC^) prompt was declined, or this account cannot install apps.
echo  Run setup.bat again and choose Yes on the prompt, or install Python 3.12+
echo  manually from  https://www.python.org/downloads/  ^(tick "Add python.exe
echo   to PATH"^), then run setup.bat again.
echo.
pause
exit /b 1

:no_winget
echo  ^(winget is not available on this PC.^)
:manual_python
echo.
echo  Please install Python 3.12+ from  https://www.python.org/downloads/
echo  ^(tick "Add python.exe to PATH"^), then run setup.bat again.
echo.
pause
exit /b 1

:have_python
echo  Using: %PYEXE%
echo.
echo  Creating the virtual environment...
%PYEXE% -m venv .venv || goto :fail

echo  Installing (this downloads a few dependencies, please wait)...
rem Editable install: the launcher runs from src/ in this folder, so updating the
rem source (e.g. git pull) takes effect without re-running setup. Keep this folder
rem in place after setup.
".venv\Scripts\python.exe" -m pip install -e . || goto :fail

if not exist "config.local.toml" copy "config.example.toml" "config.local.toml" >nul

echo.
echo  Setup complete!
echo  Double-click  AoE4-Replay-Launcher.vbs  to open the panel.
echo.
pause
exit /b 0

:fail
echo.
echo  Setup failed - see the messages above.
echo  If it mentions a Python version, install Python 3.12+ and try again.
echo.
pause
exit /b 1

rem --- find a Python 3.12+ interpreter, set PYEXE (empty if none) ---
:detect_python
set "PYEXE="
for %%V in (3.14 3.13 3.12) do (
    if not defined PYEXE (
        py -%%V -c "import sys" >nul 2>&1 && set "PYEXE=py -%%V"
    )
)
if not defined PYEXE (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1 && set "PYEXE=python"
)
goto :eof
