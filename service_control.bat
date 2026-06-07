@echo off
:: FinancialAgent — Service Control
:: Run as Administrator

set PROJECT=C:\Projects\FinancialAgent
set SERVICE_NAME=FinancialAgentScheduler

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Run as Administrator.
    pause
    exit /b 1
)

echo ============================================
echo  FinancialAgent Scheduler — Service Control
echo ============================================
echo.
echo  1. Start service
echo  2. Stop service
echo  3. Restart service
echo  4. Status
echo  5. View logs (last 50 lines)
echo  6. Uninstall service
echo  0. Exit
echo.
set /p choice=Choose: 

if "%choice%"=="1" (
    "%PROJECT%\nssm.exe" start %SERVICE_NAME%
    echo Started.
)
if "%choice%"=="2" (
    "%PROJECT%\nssm.exe" stop %SERVICE_NAME%
    echo Stopped.
)
if "%choice%"=="3" (
    "%PROJECT%\nssm.exe" restart %SERVICE_NAME%
    echo Restarted.
)
if "%choice%"=="4" (
    "%PROJECT%\nssm.exe" status %SERVICE_NAME%
)
if "%choice%"=="5" (
    echo --- scheduler.log (last 50 lines) ---
    powershell -Command "Get-Content '%PROJECT%\logs\scheduler.log' -Tail 50"
    echo.
    echo --- service_stderr.log (last 20 lines) ---
    powershell -Command "if (Test-Path '%PROJECT%\logs\service_stderr.log') { Get-Content '%PROJECT%\logs\service_stderr.log' -Tail 20 }"
)
if "%choice%"=="6" (
    set /p confirm=Uninstall service? (y/n): 
    if "%confirm%"=="y" (
        "%PROJECT%\nssm.exe" stop %SERVICE_NAME% >nul 2>&1
        "%PROJECT%\nssm.exe" remove %SERVICE_NAME% confirm
        echo Uninstalled.
    )
)

pause
