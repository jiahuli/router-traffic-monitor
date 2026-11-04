@echo off
chcp 65001 >nul
title 小米万兆路由器 · 流量监控
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [错误] 未找到 Python。请安装 Python 3.10 或更高版本，
    echo        安装时勾选 "Add Python to PATH"，然后重新双击本文件。
    echo.
    pause
    exit /b 1
)

python -c "import paramiko,sys; sys.exit(0 if int(paramiko.__version__.split('.')[0]) < 4 else 1)" >nul 2>nul
if errorlevel 1 (
    echo 正在安装/修复依赖 paramiko 3.4.1 ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败。请检查网络，或手动执行：
        echo        python -m pip install "paramiko==3.4.1"
        echo.
        pause
        exit /b 1
    )
)

echo 启动中，稍后会自动打开浏览器 ...
python app.py
pause
