# -*- coding: utf-8 -*-
"""本地 JSON 存储: 账号 / 设置 / 日志。"""
import json
import os
import re
import threading
import time
import uuid

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_lock = threading.RLock()

DEFAULT_SETTINGS = {
    "enable_daily": True,          # 每日定时签到
    "checkin_time": "09:00",       # 签到时间 HH:MM
    "auto_relogin": True,          # 登录过期时尝试自动重登 (需要保存密码)
    "check_interval": 6,           # 登录状态例行检测间隔 (小时)
    "push_on_checkin": True,       # 签到完成后推送
    "push_on_expired": True,       # 登录过期推送
    "push_hour": "",               # 推送整点: 如 "21" 只在 21 点那次执行推送; 空=每次都推
    "push": {
        "serverchan": "",          # Server酱 Turbo SendKey
        "bark": "",                # Bark Key 或完整 URL
        "pushplus": "",            # PushPlus Token
        "wecom_webhook": "",       # 企业微信机器人 Webhook
        "tg_bot_token": "",        # Telegram Bot Token
        "tg_chat_id": "",          # Telegram Chat ID
    },
}


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _path(name: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, name)


def load(name: str, default):
    with _lock:
        path = _path(name)
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            return default


def save(name: str, obj):
    with _lock:
        path = _path(name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------
def get_settings() -> dict:
    s = load("settings.json", {})
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    for k, v in (s or {}).items():
        if k == "push" and isinstance(v, dict):
            merged["push"].update(v)
        else:
            merged[k] = v
    return merged


def save_settings(new: dict) -> dict:
    cur = get_settings()
    for k in ("enable_daily", "auto_relogin", "push_on_checkin", "push_on_expired"):
        if k in new:
            cur[k] = bool(new[k])
    if "checkin_time" in new:
        t = str(new["checkin_time"]).strip()
        # 兼容浏览器时间控件带秒的情况 (HH:MM 或 HH:MM:SS), 统一归一化为 HH:MM
        m = re.match(r"^(\d{1,2}):(\d{1,2})(?::\d{1,2})?$", t)
        if not m or not (0 <= int(m.group(1)) <= 23 and int(m.group(2)) <= 59):
            raise ValueError("签到时间格式应为 HH:MM")
        cur["checkin_time"] = "%02d:%02d" % (int(m.group(1)), int(m.group(2)))
    if "check_interval" in new:
        v = str(new["check_interval"]).strip()
        if not v.isdigit() or not 1 <= int(v) <= 24:
            raise ValueError("登录检测间隔应为 1-24 之间的整数小时")
        cur["check_interval"] = int(v)
    if "push_hour" in new:
        v = str(new["push_hour"]).strip()
        if v and (not v.isdigit() or not 0 <= int(v) <= 23):
            raise ValueError("推送整点应为 0-23 的数字, 或留空表示每次都推")
        cur["push_hour"] = v
    if isinstance(new.get("push"), dict):
        for k, v in new["push"].items():
            if k in cur["push"]:
                cur["push"][k] = str(v or "").strip()
    save("settings.json", cur)
    return cur


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------
def _accounts() -> list:
    return load("accounts.json", [])


def _write_accounts(lst: list):
    save("accounts.json", lst)


def get_accounts() -> list:
    return _accounts()


def find_account(acc_id: str):
    for a in _accounts():
        if a["id"] == acc_id:
            return a
    return None


def add_account(cookies: dict, uid: str, nickname: str = "",
                password: str = None, remark: str = "") -> dict:
    accs = _accounts()
    # 同 uid 直接覆盖 (重新登录场景)
    acc = None
    if uid:
        for a in accs:
            if a.get("uid") == uid:
                acc = a
                break
    if acc is None:
        acc = {
            "id": new_id(), "uid": uid, "nickname": nickname, "remark": remark,
            "cookies": {}, "password": None, "status": "ok",
            "added_at": time.time(), "last_checkin": 0, "last_checkin_date": "",
            "last_result": None, "last_alive": 0,
        }
        accs.append(acc)
    acc["cookies"] = cookies
    acc["uid"] = uid or acc.get("uid", "")
    if nickname:
        acc["nickname"] = nickname
    if password is not None:
        acc["password"] = password
    acc["status"] = "ok"
    acc["last_alive"] = time.time()
    _write_accounts(accs)
    return acc


def update_account(acc_id: str, **fields):
    accs = _accounts()
    for a in accs:
        if a["id"] == acc_id:
            a.update(fields)
            _write_accounts(accs)
            return a
    return None


def delete_account(acc_id: str) -> bool:
    accs = _accounts()
    left = [a for a in accs if a["id"] != acc_id]
    if len(left) == len(accs):
        return False
    _write_accounts(left)
    return True


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def append_log(kind: str, message: str):
    logs = load("logs.json", [])
    logs.append({"ts": time.time(), "kind": kind, "message": message})
    save("logs.json", logs[-300:])


def get_logs(limit: int = 120) -> list:
    logs = load("logs.json", [])
    return list(reversed(logs))[:limit]
