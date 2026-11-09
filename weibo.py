# -*- coding: utf-8 -*-
"""微博超话签到核心逻辑。

包含:
- 扫码登录 (passport.weibo.com/sso/v2 二维码接口)
- Cookie 手动导入
- 实验性账号密码登录 (login.sina.com.cn 经典 RSA 流程, 可能被风控)
- 登录状态检测 (m.weibo.cn/api/config)
- 超话列表拉取 (containerid=100803_-_followsuper)
- 超话签到 (scheme -> /api/container/button)
"""
import base64
import binascii
import json
import random
import re
import time
import urllib.parse

import requests

PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
M_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def _parse_jsonp(text: str) -> dict:
    """容错解析 JSON / JSONP 响应。"""
    text = text.strip()
    m = re.search(r"\((\{.*\})\)\s*;?\s*$", text, re.S)
    if m:
        text = m.group(1)
    return json.loads(text)


def _rsa_encrypt(pubkey_hex: str, text: str) -> str:
    """微博经典 ssologin.js 的无填充 RSA 加密 (e=65537)。"""
    key = int(pubkey_hex, 16)
    m = int(binascii.hexlify(text.encode("utf-8")), 16)
    return format(pow(m, 65537, key), "0>256x")


# ---------------------------------------------------------------------------
# 扫码登录
# ---------------------------------------------------------------------------
class QRLogin:
    """发起一次微博扫码登录会话。"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": PC_UA,
            "Referer": "https://passport.weibo.com/",
            "Accept": "application/json, text/plain, */*",
        })
        r = self.session.get(
            "https://passport.weibo.com/sso/v2/qrcode/image",
            params={"size": "180"}, timeout=15)
        data = _parse_jsonp(r.text)
        if data.get("retcode") != 20000000:
            raise RuntimeError("获取登录二维码失败: %s" % data)
        self.qrid = data["data"]["qrid"]
        self.created_at = time.time()
        self.image = self._download_image(data["data"].get("image", ""))

    def _download_image(self, image_field: str) -> str:
        """image 字段可能是 base64, 也可能是二维码图片 URL, 统一转成 base64 PNG。"""
        if image_field.startswith("http"):
            for url in (image_field,
                        image_field.replace("&amp;", "&")):
                try:
                    r = self.session.get(url, timeout=15)
                    if r.ok and r.content[:4] == b"\x89PNG":
                        return base64.b64encode(r.content).decode()
                except requests.RequestException:
                    continue
            raise RuntimeError("下载二维码图片失败")
        return image_field  # 已是 base64

    def poll(self):
        """轮询扫码状态。

        返回 (status, payload):
        - ("waiting", None)   未扫码
        - ("scanned", None)   已扫码, 等待手机上确认
        - ("confirmed", cookies) 已确认, 返回 Cookie 字典
        - ("expired", None)   二维码过期
        """
        r = self.session.get(
            "https://passport.weibo.com/sso/v2/qrcode/check",
            params={"qrid": self.qrid}, timeout=15)
        try:
            data = _parse_jsonp(r.text)
        except (ValueError, json.JSONDecodeError):
            return "waiting", None
        ret = data.get("retcode")
        if ret == 20000001:
            return "waiting", None
        if ret == 20000019:
            return "scanned", None
        if ret == 20000000 and data.get("data"):
            url = data["data"].get("url") or data["data"].get("alt")
            if url:
                return "confirmed", self._exchange(url)
        if ret in (20000021, 20000022, 10005):
            return "expired", None
        return "waiting", None

    def _exchange(self, alt_url: str) -> dict:
        """访问跳转链换取全站 Cookie (PC + 移动端)。"""
        seen, queue = set(), [alt_url]
        while queue:
            u = queue.pop(0)
            if u in seen:
                continue
            seen.add(u)
            try:
                r = self.session.get(u, allow_redirects=False, timeout=15)
            except requests.RequestException:
                continue
            try:
                j = _parse_jsonp(r.text)
            except (ValueError, json.JSONDecodeError):
                continue
            for x in (j.get("crossDomainUrlList") or []):
                if x not in seen:
                    queue.append(x)
        # 激活移动端 Cookie (XSRF-TOKEN / _T_WM 等)
        try:
            self.session.get("https://m.weibo.cn/", timeout=15,
                             headers={"User-Agent": M_UA})
            self.session.get("https://m.weibo.cn/api/config", timeout=15,
                             headers={"User-Agent": M_UA})
        except requests.RequestException:
            pass
        return self.session.cookies.get_dict()


# ---------------------------------------------------------------------------
# 密码登录 (实验性)
# ---------------------------------------------------------------------------
def password_login(username: str, password: str) -> dict:
    """实验性: 账号密码登录, 成功返回 Cookie 字典。

    注意: 该流程为微博经典 SSO 登录, 可能触发风控/验证码;
    失败时请改用扫码登录。密码仅保存在本地 data/ 目录。
    """
    s = requests.Session()
    s.headers.update({"User-Agent": PC_UA, "Referer": "https://weibo.com/"})
    su = base64.b64encode(urllib.parse.quote(username).encode()).decode()
    pre = s.get(
        "https://login.sina.com.cn/sso/prelogin.php",
        params={"entry": "weibo", "callback": "sinaSSOController.preloginCallBack",
                "su": su, "rsakt": "mod", "client": "ssologin.js(v1.4.19)",
                "_": int(time.time() * 1000)},
        timeout=15)
    p = _parse_jsonp(pre.text)
    sp = _rsa_encrypt(p["pubkey"], '%s\t%s\n%s' % (p["servertime"], p["nonce"], password))
    form = {
        "entry": "weibo", "gateway": "1", "from": "", "savestate": "30",
        "useticket": "1", "pagerefer": "", "vsnf": "1", "su": su,
        "service": "sso", "servertime": p["servertime"], "nonce": p["nonce"],
        "pwencode": "rsa2", "rsakv": p.get("rsakv", ""), "sp": sp,
        "sr": "1920*1080", "encoding": "UTF-8", "cdult": "3",
        "domain": "sina.com.cn", "prelt": str(random.randint(100, 500)),
        "returntype": "TEXT",
    }
    r = s.post("https://login.sina.com.cn/sso/login.php?client=ssologin.js(v1.4.19)",
               data=form, timeout=15)
    j = _parse_jsonp(r.text)
    if str(j.get("retcode")) != "0":
        raise RuntimeError("密码登录失败: %s" % (j.get("reason") or j))
    for u in (j.get("crossDomainUrlList") or []):
        try:
            s.get(u, timeout=15)
        except requests.RequestException:
            pass
    try:
        s.get("https://m.weibo.cn/", timeout=15, headers={"User-Agent": M_UA})
    except requests.RequestException:
        pass
    return s.cookies.get_dict()


# ---------------------------------------------------------------------------
# 签到客户端
# ---------------------------------------------------------------------------
class WeiboClient:
    """携带登录 Cookie 调用微博移动端接口。"""

    def __init__(self, cookies: dict):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": M_UA,
            "Referer": "https://m.weibo.cn/",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        })
        self.session.cookies.update(cookies)

    def get_json(self, url, params=None):
        r = self.session.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    # -- 登录状态 ----------------------------------------------------------
    def login_info(self) -> dict:
        try:
            j = self.get_json("https://m.weibo.cn/api/config")
        except Exception:
            return {"login": False}
        data = j.get("data") or {}
        return {"login": bool(data.get("login")), "uid": str(data.get("uid") or "")}

    def get_nickname(self, uid: str) -> str:
        """尽力获取昵称, 失败返回空串。"""
        if not uid:
            return ""
        try:
            j = self.get_json("https://m.weibo.cn/api/container/getIndex",
                              {"containerid": "100505" + uid, "type": "uid", "value": uid})
            return (j.get("data", {}).get("userInfo", {}).get("screen_name") or "")
        except Exception:
            return ""

    # -- 超话 --------------------------------------------------------------
    def fetch_topics(self):
        """分页拉取关注的超话。

        返回 (signable, done, total):
        - signable: [{"name": 超话名, "scheme": 签到链接}]
        - done: 已签到数量
        - total: 超话总数
        """
        signable, done, total = [], 0, 0
        seen_pages = set()
        since_id = ""
        for _ in range(40):  # 最多 40 页, 防止异常循环
            params = {"containerid": "100803_-_followsuper"}
            if since_id:
                params["since_id"] = since_id
            try:
                j = self.get_json("https://m.weibo.cn/api/container/getIndex", params)
            except Exception:
                break
            if j.get("ok") != 1:
                break
            data = j.get("data") or {}
            for card in data.get("cards") or []:
                for g in card.get("card_group") or []:
                    name = (g.get("title_sub") or g.get("title") or "").strip()
                    buttons = g.get("buttons") or []
                    if not buttons:
                        continue
                    total += 1
                    names = [b.get("name", "") for b in buttons]
                    if "签到" in names:
                        scheme = next(b.get("scheme") for b in buttons
                                      if b.get("name") == "签到")
                        if scheme:
                            signable.append({"name": name, "scheme": scheme})
                    elif "已签到" in names or "明日再来" in names:
                        done += 1
            since_id = str(data.get("since_id") or "")
            if not since_id or since_id in seen_pages:
                break
            seen_pages.add(since_id)
            time.sleep(random.uniform(0.5, 1.0))
        return signable, done, total

    def sign(self, item):
        """对单个超话执行签到, 返回 (成功?, 消息)。"""
        try:
            j = self.get_json("https://m.weibo.cn" + item["scheme"])
            if j.get("ok") == 1:
                msg = ""
                tip = ((j.get("data") or {}).get("button") or {}).get("tips")
                if isinstance(tip, str):
                    msg = tip
                return True, msg
            return False, str(j.get("msg") or ("ok=%s" % j.get("ok")))
        except Exception as e:
            return False, str(e)[:120]
