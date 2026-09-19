# -*- coding: utf-8 -*-
"""示例插件 · 发言统计

演示三件事：
  1) 挂钩子：after_reply 里每次 AI 回完话就记账
  2) 自己的接口：/api/example-stats 返回统计结果
  3) 面板页：panel.html 里 fetch 这个接口把数据画出来

数据存在本插件目录下的 stats.json（自己管自己的文件，不碰核心数据）。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "stats.json")


def _read():
    try:
        return json.loads(open(DATA, encoding="utf-8").read())
    except Exception:
        return {}


def _write(d):
    with open(DATA, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)


def register(api):
    @api.hook("after_reply")
    def after_reply(p):
        """p = {session_id, mode, ai, text}"""
        d = _read()
        rec = d.setdefault(p.get("ai") or "?", {"次数": 0, "字数": 0, "最近": ""})
        text = p.get("text") or ""
        rec["次数"] += 1
        rec["字数"] += len(text)
        rec["最近"] = text[:60]
        rec["最后模式"] = p.get("mode", "")
        _write(d)

    @api.hook("before_message")
    def before_message(p):
        """演示改写：消息以「统计」开头时，顺手把统计结果塞回给 AI 参考"""
        msg = p.get("message") or ""
        if msg.startswith("统计"):
            d = _read()
            brief = "；".join(f"{k} 说了 {v['次数']} 次" for k, v in d.items())
            p["message"] = msg + f"\n（插件提供的当前统计：{brief or '暂无数据'}）"
        return p

    @api.route("GET", "/api/example-stats")
    def stats():
        return {"ok": True, "data": _read()}
