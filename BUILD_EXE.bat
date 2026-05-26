@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set APP_VERSION=V31.2

echo ============================================================
echo RunPod AI Hub %APP_VERSION% - EXE Builder
echo ============================================================
echo.
echo This build script uses the already included frontend/dist first.
echo That avoids Windows npm install crashes like:
echo   npm error Exit handler never called!
echo.
echo Result:
echo   dist\RunPod AI Hub\RunPod AI Hub.exe
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  echo Install Python 3.10+ 64-bit and enable "Add Python to PATH".
  pause
  exit /b 1
)

echo [1/4] Checking bundled frontend build...
if exist "frontend\dist\index.html" (
  echo [OK] Bundled frontend/dist found. Skipping npm install/build.
) else (
  echo [INFO] No bundled frontend/dist found. Trying to build frontend now.
  where npm >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] npm not found and frontend/dist is missing.
    echo Install Node.js LTS or use a package that already includes frontend/dist.
    pause
    exit /b 1
  )
  cd frontend
  if exist package-lock.json (
    echo Running npm ci...
    call npm ci --no-audit --no-fund
  ) else (
    echo Running npm install...
    call npm install --no-audit --no-fund
  )
  if errorlevel 1 goto :npmfail
  call npm run build
  if errorlevel 1 goto :fail
  cd ..
)

echo [2/4] Copying React build to backend\static...
if exist backend\static rmdir /s /q backend\static
mkdir backend\static
xcopy /E /I /Y frontend\dist backend\static >nul
if errorlevel 1 goto :fail

echo [3/4] Installing Python build dependencies...
python -m pip install --upgrade pip
python -m pip install --upgrade pyinstaller
python -m pip install -r backend\requirements.txt
if errorlevel 1 goto :fail

echo [4/4] Building EXE folder...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --name "RunPod AI Hub" ^
  --add-data "backend;backend" ^
  --collect-all fastapi ^
  --collect-all starlette ^
  --collect-all pydantic ^
  --collect-all pydantic_core ^
  --collect-all anyio ^
  --collect-all uvicorn ^
  --collect-all h11 ^
  --collect-all click ^
  --collect-all paramiko ^
  --collect-all cryptography ^
  --collect-all bcrypt ^
  --collect-all nacl ^
  --hidden-import fastapi.middleware ^
  --hidden-import fastapi.middleware.cors ^
  --hidden-import fastapi.responses ^
  --hidden-import fastapi.staticfiles ^
  --hidden-import starlette.middleware ^
  --hidden-import starlette.middleware.cors ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols ^
  --hidden-import uvicorn.protocols.http ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.websockets ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.lifespan ^
  --hidden-import uvicorn.lifespan.on ^
  desktop_entry.py
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo DONE! %APP_VERSION%
echo Start this file:
echo   dist\RunPod AI Hub\RunPod AI Hub.exe
echo ============================================================
echo.
pause
exit /b 0

:npmfail
cd ..
echo.
echo [ERROR] npm failed on this Windows machine.
echo This package should normally skip npm because frontend/dist is included.
echo Try deleting node_modules and npm cache, or use the included frontend/dist build.
echo.
pause
exit /b 1

:fail
echo.
echo [ERROR] Build failed. Check the messages above.
echo.
pause
exit /b 1
