@echo off
:: FinancialAgent — Install Windows Service
:: Run as Administrator

echo ============================================
echo  FinancialAgent Scheduler — Install Service
echo ============================================

:: Check admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Run this script as Administrator.
    pause
    exit /b 1
)

:: Find Python
for /f "tokens=*" %%i in ('where python') do set PYTHON=%%i
if "%PYTHON%"=="" (
    echo ERROR: Python not found in PATH.
    pause
    exit /b 1
)

set PROJECT=C:\Projects\FinancialAgent
set SERVICE_NAME=FinancialAgentScheduler
set SERVICE_DISPLAY=Financial Agent Scheduler
set SERVICE_DESC=Runs stock scans, price alerts, and Telegram notifications in the background.

echo Python: %PYTHON%
echo Project: %PROJECT%

:: Install pywin32 + nssm if needed
echo.
echo Installing pywin32...
pip install pywin32 --quiet

:: Download NSSM if not present
if not exist "%PROJECT%\nssm.exe" (
    echo.
    echo Downloading NSSM (service manager)...
    powershell -Command "Invoke-WebRequest -Uri 'https://nssm.cc/release/nssm-2.24.zip' -OutFile '%TEMP%\nssm.zip'"
    powershell -Command "Expand-Archive -Path '%TEMP%\nssm.zip' -DestinationPath '%TEMP%\nssm' -Force"
    copy "%TEMP%\nssm\nssm-2.24\win64\nssm.exe" "%PROJECT%\nssm.exe" >nul
    echo NSSM downloaded.
)

:: Remove existing service if present
"%PROJECT%\nssm.exe" status %SERVICE_NAME% >nul 2>&1
if %errorlevel% equ 0 (
    echo Removing existing service...
    "%PROJECT%\nssm.exe" stop %SERVICE_NAME% >nul 2>&1
    "%PROJECT%\nssm.exe" remove %SERVICE_NAME% confirm >nul 2>&1
)

:: Install service
echo.
echo Installing service...
"%PROJECT%\nssm.exe" install %SERVICE_NAME% "%PYTHON%" "%PROJECT%\scheduler.py"
"%PROJECT%\nssm.exe" set %SERVICE_NAME% DisplayName "%SERVICE_DISPLAY%"
"%PROJECT%\nssm.exe" set %SERVICE_NAME% Description "%SERVICE_DESC%"
"%PROJECT%\nssm.exe" set %SERVICE_NAME% AppDirectory "%PROJECT%"
"%PROJECT%\nssm.exe" set %SERVICE_NAME% AppEnvironmentExtra "PYTHONPATH=%PROJECT%"
"%PROJECT%\nssm.exe" set %SERVICE_NAME% Start SERVICE_AUTO_START
"%PROJECT%\nssm.exe" set %SERVICE_NAME% AppStdout "%PROJECT%\logs\service_stdout.log"
"%PROJECT%\nssm.exe" set %SERVICE_NAME% AppStderr "%PROJECT%\logs\service_stderr.log"
"%PROJECT%\nssm.exe" set %SERVICE_NAME% AppRotateFiles 1
"%PROJECT%\nssm.exe" set %SERVICE_NAME% AppRotateBytes 5242880

:: Start service
echo Starting service...
"%PROJECT%\nssm.exe" start %SERVICE_NAME%

echo.
echo ============================================
echo  Service installed and started.
echo  Name: %SERVICE_NAME%
echo  Logs: %PROJECT%\logs\
echo ============================================
echo.
echo To manage: use service_control.bat
pause
