@echo off
chcp 65001 >nul
cd /d %~dp0

rem ===== 面板登录账号密码, 自己改这两行 =====
set PANEL_USER=admin
set PANEL_PASSWORD=请改成你的密码

echo === 微博超话签到面板 ===
echo 访问地址: http://127.0.0.1:8787  (账号 %PANEL_USER%)
python -m pip install -r requirements.txt -q
python app.py
pause
