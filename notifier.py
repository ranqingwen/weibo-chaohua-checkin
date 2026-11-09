# -*- coding: utf-8 -*-
"""推送通知: Server酱 / PushPlus / Bark / 企业微信 / Telegram。

在设置页填入任意一个渠道的 Key 即可, 多渠道同时配置会同时推送。
"""
import urllib.parse

import requests

import store


def push(title: str, content: str, cfg: dict = None) -> list:
    """向所有已配置渠道推送, 返回 [("渠道名", 结果)] 列表。

    cfg: 渠道配置字典; 不传则读取已保存的设置。
    只推送 cfg 里非空的渠道, 因此天然支持"填了几个推几个"。
    """
    if cfg is None:
        cfg = store.get_settings().get("push", {})
    results = []

    serverchan = cfg.get("serverchan", "").strip()
    if serverchan:
        try:
            r = requests.post("https://sctapi.ftqq.com/%s.send" % serverchan,
                              data={"title": title, "desp": content}, timeout=15)
            results.append(("Server酱", "ok" if r.json().get("code") == 0 else str(r.text[:80])))
        except Exception as e:
            results.append(("Server酱", str(e)[:80]))

    pushplus = cfg.get("pushplus", "").strip()
    if pushplus:
        try:
            r = requests.post("https://www.pushplus.plus/send",
                              json={"token": pushplus, "title": title, "content": content},
                              timeout=15)
            results.append(("PushPlus", "ok" if r.json().get("code") == 200 else str(r.text[:80])))
        except Exception as e:
            results.append(("PushPlus", str(e)[:80]))

    bark = cfg.get("bark", "").strip()
    if bark:
        base = bark if bark.startswith("http") else "https://api.day.app/" + bark
        url = "%s/%s/%s" % (base.rstrip("/"),
                            urllib.parse.quote(title),
                            urllib.parse.quote(content[:180]))
        try:
            r = requests.get(url, timeout=15)
            results.append(("Bark", "ok" if r.ok else str(r.status_code)))
        except Exception as e:
            results.append(("Bark", str(e)[:80]))

    wecom = cfg.get("wecom_webhook", "").strip()
    if wecom:
        try:
            r = requests.post(wecom, json={
                "msgtype": "text",
                "text": {"content": "%s\n%s" % (title, content[:1800])},
            }, timeout=15)
            results.append(("企业微信", "ok" if r.json().get("errcode") == 0 else str(r.text[:80])))
        except Exception as e:
            results.append(("企业微信", str(e)[:80]))

    token = cfg.get("tg_bot_token", "").strip()
    chat_id = cfg.get("tg_chat_id", "").strip()
    if token and chat_id:
        try:
            r = requests.post("https://api.telegram.org/bot%s/sendMessage" % token,
                              json={"chat_id": chat_id,
                                    "text": "%s\n%s" % (title, content[:3800])},
                              timeout=15)
            results.append(("Telegram", "ok" if r.json().get("ok") else str(r.text[:80])))
        except Exception as e:
            results.append(("Telegram", str(e)[:80]))

    return results
