# -*- coding: utf-8 -*-
"""
星轨夜空皮肤 —— 示例插件（后端部分）
演示：自定义 /指令、改回复文案、统计并推送给前端、自带接口 + 插件私有存储。
改完在「设置 → 工作台插件 → ⟳ 重载插件 + 界面」即时生效。
"""
# 插件要读 POST 请求体时，参数必须标注成 Request（否则 FastAPI 会当成查询参数 → 422）
from fastapi import Request


def register(api):

    # ---- 1. 插件自带接口：面板 / 设置片段用它读状态 ----
    @api.route("GET", "/api/example-skin/stats")
    def stats():
        return {
            "ok": True,
            "plugin": api.name,
            "title": "星轨夜空皮肤",
            "sign": api.store.get("sign", True),
            "replies": api.store.get("replies", 0),
            "last": api.store.get("last"),
            "bg": api.url("bg.svg"),
        }

    @api.route("POST", "/api/example-skin/sign")
    async def set_sign(request: Request):
        body = await request.json()
        api.store.set("sign", bool(body.get("sign", True)))
        return {"ok": True, "sign": api.store.get("sign")}

    # ---- 2. 自定义指令：在任意会话里发 /skin ----
    @api.hook("command")
    def command(p):
        if p["cmd"] not in ("skin", "皮肤"):
            return None
        return {"handled": True,
                "reply": ("🌌 星轨夜空皮肤在。\n"
                          "· 背景板：{0}\n"
                          "· 回复签名：{1}\n"
                          "· 已统计回复：{2} 条".format(
                              api.url("bg.svg"),
                              "开" if api.store.get("sign", True) else "关",
                              api.store.get("replies", 0)))}

    # ---- 3. 改回复文案（before_send：推到前端前的最后一道加工）----
    @api.hook("before_send")
    def before_send(p):
        if api.store.get("sign", True) and p.get("text"):
            t = p["text"]
            # 报错信息不加签名
            if not t.startswith("[") and not t.endswith("✦ 星轨"):
                p["text"] = t + "\n\n✦ 星轨"
        return p

    # ---- 4. AI 说完后：记账 + 实时推给前端（WB.on('plugin:reply') 能收到）----
    @api.hook("after_reply")
    def after_reply(p):
        api.store.set("replies", api.store.get("replies", 0) + 1)
        api.store.set("last", {"ai": p.get("ai"), "text": (p.get("text") or "")[:80]})
        api.emit("reply", {"ai": p.get("ai"), "text": (p.get("text") or "")[:80]})
