# -*- coding: utf-8 -*-
"""微博超话签到面板 - 服务端。

运行: python app.py  (默认 http://127.0.0.1:8787)
"""
import datetime
import threading
import time
import traceback
import webbrowser

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import notifier
import store
from weibo import QRLogin, WeiboClient, password_login

app = FastAPI(title="微博超话签到面板", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
qr_store = {}          # qrid -> QRLogin
_running = set()       # 正在签到的账号 id, 防并发
_lock = threading.Lock()

COOKIE_NAME = "panel_token"
SESSION_MAX_AGE = 30 * 86400
# 无需登录即可访问的路径
PUBLIC_PATHS = {"/login", "/api/auth/status", "/api/auth/login"}


@app.middleware("http")
async def auth_middleware(request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path == "/static/weibo.svg" or request.method == "OPTIONS":
        return await call_next(request)
    if auth.check_session(request.cookies.get(COOKIE_NAME, "")):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "detail": "未登录或会话已过期"},
                            status_code=401)
    return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------------------
# 签到核心流程
# ---------------------------------------------------------------------------
def _fmt_ts(ts) -> str:
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def run_checkin_account(acc: dict, trigger: str = "手动") -> dict:
    """对单个账号执行: 登录检测 -> (过期则自动重登) -> 拉取超话 -> 签到。"""
    name = acc.get("nickname") or acc.get("remark") or ("微博用户" + (acc.get("uid") or acc["id"]))
    with _lock:
        if acc["id"] in _running:
            return {"ok": False, "msg": "该账号正在签到中"}
        _running.add(acc["id"])
    try:
        return _do_checkin(acc, trigger)
    except Exception as e:
        # 任何未预料的异常都不该变成 500 空白页, 统一转成可读消息并落日志
        traceback.print_exc()
        store.append_log("error", "账号【%s】签到异常: %s" % (name, str(e)[:200]))
        return {"ok": False, "msg": "签到异常: %s" % (str(e)[:160] or e.__class__.__name__)}
    finally:
        with _lock:
            _running.discard(acc["id"])


def _do_checkin(acc: dict, trigger: str) -> dict:
    name = acc.get("nickname") or acc.get("remark") or ("微博用户" + (acc.get("uid") or acc["id"]))
    client = WeiboClient(acc["cookies"])
    info = client.login_info()

    if not info["login"]:
        relog_ok = _try_relogin(acc, name)
        if not relog_ok:
            msg = "登录已过期，请打开签到面板重新扫码登录"
            if store.get_settings().get("push_on_expired", True) and _push_allowed():
                notifier.push("微博签到·登录过期", "账号【%s】%s" % (name, msg))
            store.append_log("expired", "账号【%s】登录已过期 (触发: %s)" % (name, trigger))
            return {"ok": False, "msg": msg, "expired": True}
        client = WeiboClient(store.find_account(acc["id"])["cookies"])
        info = client.login_info()

    signable, done, total = client.fetch_topics()
    if total == 0 and not signable:
        # 拉不到任何超话多半也是 Cookie 失效或接口异常
        store.append_log("warn", "账号【%s】未获取到任何超话数据" % name)

    result = {
        "ts": time.time(), "trigger": trigger,
        "total": total, "done_before": done,
        "signable": len(signable), "success": 0, "failed": [],
    }

    # 没有可签的超话: 全部已签(或本来就无可签), 不重复请求签到接口, 直接给出结论
    if not signable:
        store.update_account(
            acc["id"], last_checkin=time.time(),
            last_checkin_date=datetime.date.today().isoformat(),
            last_result=result, status="ok", last_alive=time.time())
        if done > 0:
            store.append_log("checkin", "账号【%s】今日 %d 个超话已全部签到, 无需重复签到"
                             % (name, done))
            return {"ok": True, "result": result, "all_done": True,
                    "msg": "今日 %d 个超话已全部签到 ✓，无需重复签到" % done}
        store.append_log("warn", "账号【%s】未发现可签到的超话 (共%d个)" % (name, total))
        return {"ok": True, "result": result,
                "msg": "未发现可签到的超话（共 %d 个）" % total}

    success, failed = 0, []
    for i, t in enumerate(signable, 1):
        ok, msg = client.sign(t)
        if ok:
            success += 1
        else:
            failed.append("%s: %s" % (t["name"] or "未命名超话", msg))
        time.sleep(0.8)  # 控制频率, 避免触发风控

    result["success"] = success
    result["failed"] = failed
    acc = store.update_account(
        acc["id"], last_checkin=time.time(),
        last_checkin_date=datetime.date.today().isoformat(),
        last_result=result, status="ok", last_alive=time.time())
    store.append_log("checkin", "账号【%s】签到完成: 共%d个超话, 已签%d, 新签%d, 失败%d"
                     % (name, total, done, success, len(failed)))

    if store.get_settings().get("push_on_checkin", True) and _push_allowed():
        content = ("账号: %s\n触发方式: %s\n\n"
                   "签到结果: 共 %d 个超话\n"
                   "├ 之前已签: %d 个\n"
                   "├ 本次成功: %d 个\n"
                   "└ 本次失败: %d 个"
                   % (name, trigger, total, done, success, len(failed)))
        if failed:
            content += "\n\n失败明细:\n" + "\n".join("- " + f for f in failed[:10])
        notifier.push("微博超话签到: 共%d 成功%d 失败%d"
                      % (total, success, len(failed)), content)

    return {"ok": True, "result": result}


def _try_relogin(acc: dict, name: str) -> bool:
    """登录过期时尝试自动重登 (仅当保存了密码且开关开启)。"""
    settings = store.get_settings()
    pwd = acc.get("password")
    if not (settings.get("auto_relogin") and pwd):
        return False
    # 密码重登需要用户名; 微博密码登录支持手机号/邮箱
    username = acc.get("login_name") or acc.get("remark") or ""
    if not username:
        return False
    store.append_log("info", "账号【%s】登录过期, 正在尝试密码自动重登..." % name)
    try:
        cookies = password_login(username, pwd)
    except Exception as e:
        store.append_log("error", "账号【%s】自动重登失败: %s" % (name, e))
        if settings.get("push_on_expired", True) and _push_allowed():
            notifier.push("微博签到·自动重登失败",
                          "账号【%s】登录过期且密码自动重登失败:\n%s\n\n请打开面板重新扫码登录。" % (name, e))
        return False
    client = WeiboClient(cookies)
    info = client.login_info()
    if not info["login"]:
        return False
    store.update_account(acc["id"], cookies=cookies, uid=info.get("uid") or acc.get("uid"),
                         status="ok", last_alive=time.time())
    store.append_log("info", "账号【%s】密码自动重登成功" % name)
    if store.get_settings().get("push_on_expired", True) and _push_allowed():
        notifier.push("微博签到·自动重登成功", "账号【%s】登录过期后已自动重新登录, 签到继续。" % name)
    return True


def _push_allowed() -> bool:
    """推送整点控制: 设置了 push_hour 时, 只有当前小时等于该值才推送。"""
    h = str(store.get_settings().get("push_hour", "") or "").strip()
    if h == "":
        return True
    try:
        return datetime.datetime.now().hour == int(h)
    except ValueError:
        return True


def run_checkin_all(trigger: str = "定时"):
    """全部账号依次签到 (跳过今天已签的, 实现补签逻辑)。"""
    today = datetime.date.today().isoformat()
    for acc in store.get_accounts():
        if acc.get("last_checkin_date") == today and trigger == "定时":
            continue
        try:
            run_checkin_account(acc, trigger)
        except Exception as e:
            store.append_log("error", "账号签到异常: %s" % e)


def expiry_monitor():
    """定期检测所有账号登录状态; 过期则尝试重登并推送。"""
    settings = store.get_settings()
    for acc in store.get_accounts():
        client = WeiboClient(acc["cookies"])
        info = client.login_info()
        if info["login"]:
            store.update_account(acc["id"], status="ok", last_alive=time.time())
            continue
        name = acc.get("nickname") or acc.get("remark") or ("微博用户" + (acc.get("uid") or acc["id"]))
        if _try_relogin(acc, name):
            store.update_account(acc["id"], status="ok")
            continue
        if acc.get("status") != "expired":  # 只在状态变化时推送一次
            store.update_account(acc["id"], status="expired")
            store.append_log("expired", "账号【%s】登录已过期 (例行检测)" % name)
            if settings.get("push_on_expired", True) and _push_allowed():
                notifier.push("微博签到·登录过期",
                              "账号【%s】登录已过期, 请打开签到面板重新扫码登录。" % name)


def _daily_job():
    run_checkin_all("定时")


def _startup_makeup():
    """启动补签: 当天签到时间已过但还没签, 立即补一次。"""
    settings = store.get_settings()
    if not settings.get("enable_daily"):
        return
    try:
        h, m = map(int, settings["checkin_time"].split(":"))
    except (ValueError, AttributeError):
        return
    now = datetime.datetime.now()
    today = now.date().isoformat()
    due = any(a.get("last_checkin_date") != today for a in store.get_accounts())
    if now.hour * 60 + now.minute >= h * 60 + m and due:
        store.append_log("info", "检测到当日签到时间已过, 执行补签...")
        threading.Thread(target=run_checkin_all, args=("补签",), daemon=True).start()


def schedule_all():
    """根据设置注册/刷新定时任务。"""
    settings = store.get_settings()
    if settings.get("enable_daily"):
        h, m = map(int, settings["checkin_time"].split(":"))
        scheduler.add_job(_daily_job, CronTrigger(hour=h, minute=m),
                          id="daily_checkin", replace_existing=True)
    else:
        job = scheduler.get_job("daily_checkin")
        if job:
            job.remove()
    scheduler.add_job(expiry_monitor, "interval", hours=6,
                      id="expiry_monitor", replace_existing=True)


# ---------------------------------------------------------------------------
# Web API
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    auth.init()                  # 从环境变量读取面板账号密码
    scheduler.start()
    schedule_all()
    _startup_makeup()


@app.get("/")
def index():
    return FileResponse("static/index.html")


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
@app.get("/login")
def login_page():
    return FileResponse("static/login.html")


class LoginBody(BaseModel):
    username: str
    password: str


def _auth_response(token: str = None, msg: str = ""):
    resp = JSONResponse({"ok": bool(token), "msg": msg})
    if token:
        resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_MAX_AGE,
                        httponly=True, samesite="lax")
    return resp


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    """登录页探测: 当前是否已登录 / 是否处于随机密码模式。"""
    authed = auth.check_session(request.cookies.get(COOKIE_NAME, ""))
    return {"ok": True, "authed": authed, "random_mode": auth.is_random_mode()}


@app.post("/api/auth/login")
def api_auth_login(body: LoginBody):
    if not auth.is_configured():
        raise HTTPException(503, "面板密码尚未初始化, 请重启服务")
    if not auth.verify(body.username, body.password):
        store.append_log("warn", "面板登录失败: 用户名 %s" % (body.username[:16]))
        raise HTTPException(401, "账号或密码错误")
    store.append_log("info", "面板登录成功: %s" % body.username[:16])
    return _auth_response(auth.new_session())


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    token = request.cookies.get(COOKIE_NAME, "")
    if token:
        auth.revoke_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/api/accounts")
def api_accounts():
    out = []
    for a in store.get_accounts():
        out.append({
            "id": a["id"], "uid": a.get("uid", ""),
            "nickname": a.get("nickname", ""), "remark": a.get("remark", ""),
            "status": a.get("status", "ok"), "has_password": bool(a.get("password")),
            "added_at": a.get("added_at"), "last_checkin": a.get("last_checkin"),
            "last_checkin_date": a.get("last_checkin_date", ""),
            "last_alive": a.get("last_alive"), "last_result": a.get("last_result"),
        })
    return {"ok": True, "accounts": out}


@app.delete("/api/accounts/{acc_id}")
def api_delete_account(acc_id: str):
    if not store.delete_account(acc_id):
        raise HTTPException(404, "账号不存在")
    store.append_log("info", "删除账号 %s" % acc_id)
    return {"ok": True}


@app.post("/api/accounts/{acc_id}/checkin")
def api_checkin(acc_id: str):
    acc = store.find_account(acc_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    res = run_checkin_account(acc, "手动")
    return res


@app.post("/api/accounts/{acc_id}/detect")
def api_detect(acc_id: str):
    """只检测签到状态, 不执行签到 (不会产生任何签到请求)。"""
    acc = store.find_account(acc_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    name = acc.get("nickname") or acc.get("remark") or ("微博用户" + (acc.get("uid") or acc_id))
    try:
        client = WeiboClient(acc["cookies"])
        info = client.login_info()
        if not info["login"]:
            store.update_account(acc_id, status="expired", last_alive=time.time())
            return {"ok": False, "msg": "登录已过期，请重新扫码登录", "expired": True}
        d = client.detect()
        store.update_account(acc_id, status="ok", last_alive=time.time())
        store.append_log("info", "账号【%s】签到检测: 共%d个超话, 已签%d, 待签%d"
                         % (name, d["total"], d["done"], len(d["pending"])))
        return {"ok": True, **d}
    except Exception as e:
        traceback.print_exc()
        store.append_log("error", "账号【%s】检测异常: %s" % (name, str(e)[:200]))
        return {"ok": False, "msg": "检测失败: %s" % (str(e)[:160] or e.__class__.__name__)}


@app.post("/api/accounts/{acc_id}/check_login")
def api_check_login(acc_id: str):
    acc = store.find_account(acc_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    client = WeiboClient(acc["cookies"])
    info = client.login_info()
    if info["login"]:
        store.update_account(acc_id, status="ok", last_alive=time.time(),
                             uid=info.get("uid") or acc.get("uid"))
        if not acc.get("nickname"):
            nick = client.get_nickname(info.get("uid", ""))
            if nick:
                store.update_account(acc_id, nickname=nick)
        return {"ok": True, "login": True}
    store.update_account(acc_id, status="expired")
    return {"ok": True, "login": False}


@app.post("/api/accounts/{acc_id}/remark")
def api_set_remark(acc_id: str, body: dict):
    acc = store.find_account(acc_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    store.update_account(acc_id, remark=str(body.get("remark", ""))[:20])
    return {"ok": True}


@app.post("/api/checkin/all")
def api_checkin_all():
    threading.Thread(target=run_checkin_all, args=("手动",), daemon=True).start()
    return {"ok": True, "msg": "已开始全部签到, 稍后刷新查看结果"}


# -- 扫码登录 -----------------------------------------------------------------
@app.post("/api/login/qrcode")
def api_qr_create():
    try:
        qr = QRLogin()
    except Exception as e:
        raise HTTPException(502, "获取二维码失败: %s" % e)
    qr_store[qr.qrid] = qr
    # 清理超过 10 分钟的旧会话
    now = time.time()
    for k in list(qr_store):
        if now - qr_store[k].created_at > 600:
            qr_store.pop(k, None)
    return {"ok": True, "qrid": qr.qrid, "image": qr.image}


@app.get("/api/login/qrcode/check")
def api_qr_check(qrid: str):
    qr = qr_store.get(qrid)
    if not qr:
        return {"ok": False, "status": "expired", "msg": "二维码已失效, 请重新获取"}
    try:
        status, payload = qr.poll()
    except Exception as e:
        return {"ok": False, "status": "failed", "msg": "轮询异常: %s" % str(e)[:80]}
    if status != "confirmed":
        return {"ok": True, "status": status}
    qr_store.pop(qrid, None)
    last = getattr(qr, "last", {}) or {}
    if not payload:
        store.append_log("login", "扫码确认后换取 Cookie 失败 (retcode=%s)" % last.get("retcode"))
        return {"ok": False, "status": "failed",
                "msg": "已确认但换取登录 Cookie 失败, 请重试"}
    if not any(k in payload for k in ("SUB", "SUBP", "ALF")):
        store.append_log("login", "扫码返回的 Cookie 不完整: %s" % ",".join(list(payload)[:8]))
        return {"ok": False, "status": "failed", "msg": "登录 Cookie 不完整, 请重试"}
    client = WeiboClient(payload)
    info = client.login_info()
    if not info["login"]:
        store.append_log("login", "扫码 Cookie 校验未通过 (retcode=%s)" % last.get("retcode"))
        return {"ok": False, "status": "failed", "msg": "登录 Cookie 校验未通过, 请重试"}
    uid = info.get("uid", "")
    nick = client.get_nickname(uid)
    acc = store.add_account(payload, uid=uid, nickname=nick)
    store.append_log("login", "扫码登录成功: %s" % (nick or uid))
    return {"ok": True, "status": "confirmed",
            "account": {"id": acc["id"], "nickname": nick or uid}}


# -- Cookie 登录 ---------------------------------------------------------------
class CookieBody(BaseModel):
    cookie: str


@app.post("/api/login/cookie")
def api_login_cookie(body: CookieBody):
    raw = body.cookie.strip().strip('"')
    cookies = {}
    for part in raw.replace("\n", ";").split(";"):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            if k:
                cookies[k] = v
    if not cookies:
        raise HTTPException(400, "Cookie 格式不正确")
    client = WeiboClient(cookies)
    info = client.login_info()
    if not info["login"]:
        return {"ok": False, "msg": "Cookie 无效 (未登录), 请重新获取"}
    uid = info.get("uid", "")
    nick = client.get_nickname(uid)
    acc = store.add_account(cookies, uid=uid, nickname=nick)
    store.append_log("login", "Cookie 导入成功: %s" % (nick or uid))
    return {"ok": True, "account": {"id": acc["id"], "nickname": nick or uid}}


# -- 密码登录 (实验性) -----------------------------------------------------------
class PasswordBody(BaseModel):
    username: str
    password: str
    remember: bool = True


@app.post("/api/login/password")
def api_login_password(body: PasswordBody):
    try:
        cookies = password_login(body.username.strip(), body.password)
    except Exception as e:
        store.append_log("error", "密码登录失败(%s): %s" % (body.username[:4] + "***", e))
        return {"ok": False, "msg": str(e)}
    client = WeiboClient(cookies)
    info = client.login_info()
    if not info["login"]:
        return {"ok": False, "msg": "登录返回的 Cookie 未通过校验, 可能触发风控, 请改用扫码登录"}
    uid = info.get("uid", "")
    nick = client.get_nickname(uid)
    acc = store.add_account(cookies, uid=uid, nickname=nick,
                            password=body.password if body.remember else None)
    if body.remember:
        store.update_account(acc["id"], login_name=body.username.strip())
    store.append_log("login", "密码登录成功: %s%s" % (nick or uid,
                                                     " (已记住密码用于自动重登)" if body.remember else ""))
    return {"ok": True, "account": {"id": acc["id"], "nickname": nick or uid}}


# -- 设置 / 日志 ----------------------------------------------------------------
@app.get("/api/settings")
def api_get_settings():
    return {"ok": True, "settings": store.get_settings()}


@app.post("/api/settings")
def api_set_settings(body: dict):
    try:
        s = store.save_settings(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    schedule_all()
    store.append_log("info", "设置已更新")
    return {"ok": True, "settings": s}


class PushTestBody(BaseModel):
    push: dict = {}   # 当前表单里填的渠道配置, 不需要先保存


@app.post("/api/push/test")
def api_push_test(body: PushTestBody = None):
    """用当前填写的配置立即发测试消息; 未填写的渠道跳过; 不受推送整点限制。"""
    if body and any(str(v or "").strip() for v in body.push.values()):
        valid = store.DEFAULT_SETTINGS["push"].keys()
        cfg = {k: str(body.push.get(k) or "").strip() for k in valid}
    else:
        cfg = store.get_settings().get("push", {})
    if not any(cfg.values()):
        return {"ok": False, "msg": "还没有填写任何推送 Key, 请先填写"}
    results = notifier.push("微博签到面板·测试推送",
                            "这是一条测试消息。收到即说明该渠道配置成功 ✓\n\n"
                            "之后签到完成和登录过期提醒都会推送到这里。", cfg=cfg)
    def _brief(r: str) -> str:
        """渠道返回的错误可能是一大段 JSON, 日志里只保留可读摘要。"""
        r = " ".join(str(r).split())
        return r if len(r) <= 60 else r[:60] + "…"

    store.append_log("info", "测试推送: %s" %
                     (", ".join("%s=%s" % (n, _brief(r)) for n, r in results) or "无"))
    return {"ok": True, "results": results}


@app.get("/api/logs")
def api_logs():
    return {"ok": True, "logs": store.get_logs()}


@app.get("/api/stats")
def api_stats():
    today = datetime.date.today().isoformat()
    accs = store.get_accounts()
    checked_accs = [a for a in accs if a.get("last_checkin_date") == today]
    expired = sum(1 for a in accs if a.get("status") == "expired")
    # 今日签到超话口径: 新签成功数 + 之前已签数 (今天实际处于已签状态的超话)
    success = sum(int((a.get("last_result") or {}).get("success") or 0)
                  + int((a.get("last_result") or {}).get("done_before") or 0)
                  for a in checked_accs)
    failed = sum(len((a.get("last_result") or {}).get("failed") or [])
                 for a in checked_accs)
    return {"ok": True, "accounts": len(accs),
            "checked_today": len(checked_accs),
            "today_success": success, "today_failed": failed,
            "expired": expired}


if __name__ == "__main__":
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8787")).start()
    uvicorn.run(app, host="0.0.0.0", port=8787, log_level="info")
