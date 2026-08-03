@echo off
chcp 65001 >nul
title SceneForge
cd /d "%~dp0"

rem ============ 可按需修改 ============
rem 端口
set "PORT=8770"
rem 访问令牌：留空 = 仅本机、无需令牌；要开放给别人就填一串字符（例如 mySecret123）
set "SCENEFORGE_WEB_TOKEN="
rem 监听地址：127.0.0.1 = 只本机；0.0.0.0 = 同局域网他人也能访问（务必同时设上面的令牌）
set "HOST=127.0.0.1"
rem ===================================

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未找到虚拟环境 .venv\Scripts\python.exe
  echo 请先在本目录（SceneForge）执行:  uv sync
  echo.
  pause
  exit /b 1
)

if not exist "webui-dist\index.html" (
  echo 未找到 Vue 前端构建产物，正在检查构建环境 ...
  where npm >nul 2>nul
  if errorlevel 1 (
    echo [错误] 未找到 npm。请安装 Node.js，然后执行:
    echo   cd frontend
    echo   npm install
    echo   npm run build
    echo.
    pause
    exit /b 1
  )
  if not exist "frontend\node_modules\.bin\vite.cmd" (
    echo [错误] 前端依赖尚未安装。请先执行:
    echo   cd frontend
    echo   npm install
    echo   npm run build
    echo.
    pause
    exit /b 1
  )
  pushd frontend
  call npm run build
  if errorlevel 1 (
    popd
    echo [错误] Vue 前端构建失败，请检查上方输出。
    pause
    exit /b 1
  )
  popd
)

rem 端口可能仍被上一次运行的旧版 SceneForge 占用。先检查资产接口；
rem 新版服务可直接复用，旧版 main_server.py 则安全重启，其他程序绝不误杀。
powershell -NoProfile -Command ^
  "$h = @{}; if ($env:SCENEFORGE_WEB_TOKEN) { $h.Authorization = 'Bearer ' + $env:SCENEFORGE_WEB_TOKEN }; try { $r = Invoke-WebRequest -UseBasicParsing -Headers $h -Uri 'http://127.0.0.1:%PORT%/api/assets?asset_type=prop' -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1"
if not errorlevel 1 (
  echo SceneForge 已在运行，正在打开页面 ...
  start "" explorer http://127.0.0.1:%PORT%/
  exit /b 0
)

set "PORT_PID="
for /f %%P in ('powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue ^| Select-Object -First 1; if ($c) { $c.OwningProcess }"') do set "PORT_PID=%%P"
if defined PORT_PID (
  powershell -NoProfile -Command ^
    "$p = Get-CimInstance Win32_Process -Filter 'ProcessId = %PORT_PID%' -ErrorAction SilentlyContinue; if (-not $p -or $p.CommandLine -notmatch 'main_server\.py') { exit 2 }; Stop-Process -Id %PORT_PID% -Force; Start-Sleep -Milliseconds 500"
  if errorlevel 1 (
    echo [错误] 端口 %PORT% 已被其他程序占用，请关闭该程序或修改 start.bat 中的 PORT。
    pause
    exit /b 1
  )
  echo 已关闭占用端口 %PORT% 的旧版 SceneForge 服务。
)

echo 正在启动 SceneForge ...
echo   地址: http://127.0.0.1:%PORT%/
if defined SCENEFORGE_WEB_TOKEN if not "%SCENEFORGE_WEB_TOKEN%"=="" echo   访问令牌: %SCENEFORGE_WEB_TOKEN%
echo   关闭此窗口即可停止服务。
echo.

rem 3 秒后自动用默认浏览器打开（不阻塞服务）
start "" /min cmd /c "timeout /t 3 >nul & explorer http://127.0.0.1:%PORT%/"

".venv\Scripts\python.exe" main_server.py --port %PORT% --host %HOST%

echo.
echo 服务已停止。
pause
