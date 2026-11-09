# -*- coding: utf-8 -*-
"""面板访问认证: 账号 + 密码 (环境变量配置) + 会话 Token。

- PANEL_USER      登录账号, 默认 admin
- PANEL_PASSWORD  登录密码, Docker 部署时必须配置;
                  未配置时启动会生成随机密码并打印在控制台
- 会话 Token 存 data/auth.json, 重启不掉线
"""
import os
import secrets
import time

import store

ENV_USER = "PANEL_USER"
ENV_PASSWORD = "PANEL_PASSWORD"
SESSION_DAYS = 30

_user = "admin"
_password = None      # None 表示尚未初始化
_random_mode = False


def init():
    """服务启动时调用: 从环境变量读取登录凭据。"""
    global _user, _password, _random_mode
    _user = os.environ.get(ENV_USER, "").strip() or "admin"
    env_pwd = os.environ.get(ENV_PASSWORD, "").strip()
    if env_pwd:
        _password = env_pwd
        _random_mode = False
    else:
        _password = secrets.token_urlsafe(8)
        _random_mode = True
        msg = "[安全提示] 未设置 PANEL_PASSWORD 环境变量, 本次启动已生成随机密码 -> 用户名: %s  密码: %s" % (
            _user, _password)
        print(msg, flush=True)
        store.append_log("warn", "未设置 PANEL_PASSWORD, 已生成随机密码 (见启动控制台)")


def is_configured() -> bool:
    return _password is not None


def get_username() -> str:
    return _user


def is_random_mode() -> bool:
    return _random_mode


def verify(username: str, password: str) -> bool:
    if _password is None:
        return False
    ok_user = secrets.compare_digest((username or "").strip(), _user)
    ok_pwd = secrets.compare_digest(password or "", _password)
    return ok_user and ok_pwd


# ---------------------------------------------------------------------------
# 会话 (存 data/auth.json, 重启不掉线)
# ---------------------------------------------------------------------------
def _load_sessions() -> dict:
    return store.load("auth.json", {}).get("sessions", {})


def _save_sessions(sessions: dict):
    store.save("auth.json", {"sessions": sessions})


def new_session() -> str:
    sessions = _load_sessions()
    token = secrets.token_urlsafe(32)
    sessions[token] = time.time() + SESSION_DAYS * 86400
    now = time.time()
    _save_sessions({k: v for k, v in sessions.items() if v > now})
    return token


def check_session(token: str) -> bool:
    if not token:
        return False
    exp = _load_sessions().get(token)
    return bool(exp and exp > time.time())


def revoke_session(token: str):
    sessions = _load_sessions()
    sessions.pop(token, None)
    _save_sessions(sessions)
