@echo off
chcp 65001 >nul
rem Lance l'application Bordereaux (double-clic).
where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw -3 "%~dp0bordereaux.pyw"
  exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw "%~dp0bordereaux.pyw"
  exit /b 0
)
echo Python n'est pas installe sur ce poste. Voir LISEZMOI.txt (section Installation).
echo.
pause
