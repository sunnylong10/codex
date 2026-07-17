@echo off
setlocal
REM ============================================================
REM  One-click MNC job pipeline — press ENTER to accept defaults
REM ============================================================
cd /d "%~dp0"
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat

REM -- Anthropic API key (for AI self-healing rounds) --------------
if not "%ANTHROPIC_API_KEY%"=="" goto havekey
echo.
echo No ANTHROPIC_API_KEY found in your environment.
echo Paste your key below (right-click to paste), or press ENTER to
echo skip - healing rounds then use free sniff discovery instead.
set /p ANTHROPIC_API_KEY=API key: 
:havekey
if "%ANTHROPIC_API_KEY%"=="" (
    echo   - no key: healing rounds will use free sniff discovery
) else (
    echo   - API key loaded: AI healing enabled
)

REM --- Anthropic API key (optional, enables AI re-discovery in fix rounds) ---
REM Priority: already-set env var > anthropic_key.txt in this folder > prompt
if "%ANTHROPIC_API_KEY%"=="" if exist anthropic_key.txt set /p ANTHROPIC_API_KEY=<anthropic_key.txt
if "%ANTHROPIC_API_KEY%"=="" (
    echo.
    echo Optional: paste your Anthropic API key to enable AI re-discovery
    echo   ^(right-click to paste in this window; or save it once into a file
    echo    named anthropic_key.txt next to this script and never be asked again^)
    echo Press ENTER to skip and use free sniff discovery instead.
    set /p ANTHROPIC_API_KEY=API key [skip]: 
)
if "%ANTHROPIC_API_KEY%"=="" (echo Using free sniff discovery for fix rounds.) else (echo AI re-discovery enabled.)

set W=3
set JMIN=0.4
set JMAX=1.6
set MAXJ=400
set ROUNDS=2
set FRESHQ=n
set LOC=Singapore

echo.
set /p W=Parallel browser workers [3]: 
set /p JMIN=Jitter minimum seconds [0.4]: 
set /p JMAX=Jitter maximum seconds [1.6]: 
set /p MAXJ=Max newest jobs per company, 0=unlimited [400]: 
set /p ROUNDS=Self-heal fix rounds [2]: 
set /p LOC=Country filter: "all", one country, or comma list e.g. Singapore,Hong Kong [Singapore]: 
set /p FRESHQ=Forget the retry blacklist and start fresh? y/N [n]: 

set FRESH=
if /i "%FRESHQ%"=="y" set FRESH=--fresh
set LOCPART=--location \"%LOC%\"
if /i "%LOC%"=="all" set LOCPART=--all-locations

echo.
echo Running: workers=%W%  jitter=%JMIN%-%JMAX%s  cap=%MAXJ%  rounds=%ROUNDS%  location=%LOC%  %FRESH%
echo ------------------------------------------------------------
python pipeline.py --max-rounds %ROUNDS% %FRESH% --scraper-args "--workers %W% --jitter %JMIN% %JMAX% --max-per-company %MAXJ% %LOCPART%"

echo.
echo ================= PIPELINE FINISHED =================
pause
