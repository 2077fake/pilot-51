@echo off
setlocal
rem 51 单片机在线调试台启动器：双击本文件即可，服务起来后会自动打开浏览器。
rem 两个坑（round-02 实测）：
rem 1) 本文件必须存成 ANSI/GBK。cmd 按系统 ANSI 代码页逐行解析批处理，存 UTF-8 会让中文 echo 变乱码。
rem 2) 这里不要写 nul 重定向，也不要 chcp 65001——控制台留在默认代码页，
rem    Python 自己会用控制台接口输出中文，强行切 65001 反而容易把日志搞花。

cd /d "%~dp0webui"
if not exist server.py (
    echo [错误] 找不到 webui 里的 server.py，请把本文件和 webui 目录放在一起。
    pause
    exit /b 1
)

echo 使用的 Python：
where python
if errorlevel 1 (
    echo [错误] PATH 里没有 python。后端只用标准库，装个 Python 3.8+ 就够。
    pause
    exit /b 1
)

echo.
echo 正在启动调试台：http://127.0.0.1:8765
echo 关掉这个窗口就是停止服务，浏览器里的推送会跟着断开。
echo.
python server.py
echo.
echo 后端已退出。上面若有报错，看完再关窗口。
pause
endlocal
