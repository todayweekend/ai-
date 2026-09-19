# -*- coding: utf-8 -*-
"""
工作台插件：小鲸鱼余额挂件（移植自 DSH 的 dsh-whale-widget，MIT 许可）
================================================================
在 localhost:7860 界面右下角浮一只小鲸鱼，显示余额 + 今日已用，
每 60 秒自动刷新、点击手动刷新；余额变动有滚动动画。

多账户
------
一个「账户」= 名称 + 余额接口地址 + API Key。可以同时挂：
  · 同一个平台的多个 Key（比如 DeepSeek 的项目号、个人号各一个）
  · 不同平台（DeepSeek / OpenRouter / 硅基流动 / 自填地址的其它平台）
挂件上一次显示一个账户，点鲸鱼切换到下一个。

· 余额：在服务端拉取，Key 不下发给前端（前端只拿到打码串）。
· 今日已用：记账模式（观测余额下降就累加，跨天归零归档），按账户分别记，
  不需要平台的用量接口。
· 峰谷：按北京时间判定（工作日 9-12 / 14-18 高峰，周末谷价），仅用于提示文案。

关于「其它平台」：Kimi / 智谱 / 通义 / 豆包等目前没有公开的余额查询接口，
所以它们只能填自定义地址；填不上就老老实实标注「该平台未提供余额接口」，
不假装能查。
"""
import os
import json
import time
import uuid
import datetime
import urllib.request
import urllib.error

# 读 POST 请求体时，处理函数的参数必须标注成 Request，
# 否则 FastAPI 会把它当成一个必填查询参数 → 422 Unprocessable Content。
from fastapi import Request

BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
BALANCE_TTL = 25          # 秒：服务端缓存，避免频繁打平台接口
# 北京时间 2026-08-23 00:00 的 UTC 秒（此时间点起周末全天按谷价）
WEEKEND_VALLEY_FROM = 1787414400
# 注：DeepSeek 2026-09-19 补充说明 —— 调休上班的周末、法定节假日同样全天按空闲时段计费。
#     这里按周六/周日判定，已覆盖调休周末；法定节假日中夹着的工作日未单列。

DEFAULT_ACCOUNT_ID = "__default__"

# 常见平台的余额接口预设（只列真的查得到的）
PLATFORMS = [
    {"id": "deepseek", "name": "DeepSeek",
     "url": "https://api.deepseek.com/user/balance",
     "hint": "官方余额接口，开箱可用"},
    {"id": "openrouter", "name": "OpenRouter",
     "url": "https://openrouter.ai/api/v1/credits",
     "hint": "返回总额度与已用量，自动相减"},
    {"id": "siliconflow", "name": "硅基流动",
     "url": "https://api.siliconflow.cn/v1/user/info",
     "hint": "部分账号可读"},
    {"id": "custom", "name": "自定义 / 其它平台",
     "url": "",
     "hint": "自己填接口地址。Kimi / 智谱 / 通义 / 豆包等暂无公开余额接口"},
]


def _now_sec():
    return int(time.time())


def _is_peak_time(sec):
    """北京时间峰谷判定（与 DSH 原版一致）"""
    n = int(sec)
    bj = datetime.datetime.utcfromtimestamp(n + 8 * 3600)
    if n >= WEEKEND_VALLEY_FROM and bj.weekday() >= 5:   # 5=周六 6=周日
        return False
    h = bj.hour
    return (9 <= h < 12) or (14 <= h < 18)


def _today_str():
    """北京时间算「今天」，与峰谷判定同一时区，跨时区机器上跨天边界才不会错位。"""
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")


def _load_workbench_config():
    try:
        with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _mask(key):
    if not key:
        return ""
    k = str(key)
    if len(k) <= 10:
        return k[:2] + "…"
    return k[:6] + "…" + k[-4:]


def _fetch(url, api_key):
    """带 Bearer 拉一次；返回解析后的 dict（各家结构不同，交给 _parse 认）"""
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
    })
    # 用 urllib 默认网络栈（不强制任何代理）：普通直连直接通；
    # 内网环境设 https_proxy 环境变量即走代理；本机 WinINET 代理异常也不受影响。
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _parse(d):
    """从各家返回里抠出余额。返回 (总额, 币种, 明细 dict)。

    认结构的顺序：DeepSeek → OpenRouter → 通用字段。
    认不出来就抛异常，让界面老实显示「认不出」而不是瞎报一个数。
    """
    if not isinstance(d, dict):
        raise ValueError("接口没返回 JSON 对象")

    # 1) DeepSeek: {"balance_infos":[{"total_balance","granted_balance","voucher_balance"}]}
    infos = d.get("balance_infos")
    if isinstance(infos, list) and infos and isinstance(infos[0], dict):
        inf = infos[0]
        bal = float(inf.get("total_balance") or 0)
        granted = float(inf.get("granted_balance") or 0)
        voucher = float(inf.get("voucher_balance") or 0)
        extra = {}
        if granted:
            extra["赠送"] = granted
        if voucher:
            extra["代金券"] = voucher
        return bal + granted + voucher, (inf.get("currency") or "CNY"), extra

    # 2) OpenRouter: {"data":{"total_credits":10,"total_usage":3.2}} → 相减
    data = d.get("data") if isinstance(d.get("data"), dict) else d
    tc, tu = data.get("total_credits"), data.get("total_usage")
    if tc is not None and tu is not None:
        try:
            return (float(tc) - float(tu)), "USD", {"总额度": float(tc), "已用": float(tu)}
        except Exception:
            pass

    # 3) 通用字段（硅基流动等）
    for k in ("total_balance", "totalBalance", "available_balance", "balance",
              "remain", "available", "total_available"):
        v = data.get(k, d.get(k))
        if v is None:
            continue
        try:
            return float(v), (data.get("currency") or d.get("currency") or "CNY"), {}
        except Exception:
            continue

    raise ValueError("认不出这个接口的余额字段（返回的键：%s）"
                     % ", ".join(list(data.keys())[:8]))


def register(api):
    store = api.store   # 插件私有 KV（plugins/whale-widget/.data.json，重启不丢）

    # ------------------------------------------------------------------ 账户
    def _saved_accounts():
        acc = store.get("accounts")
        return acc if isinstance(acc, list) else []

    def _default_account():
        """还没配过账户时的兜底：用工作台 config.json 里的 DeepSeek Key。

        这样老用户升级后不用重新填一遍 —— 挂件照旧能用。
        """
        cfg = _load_workbench_config()
        key = ((cfg.get("deepseek") or {}).get("api_key") or "")
        if not key:
            return None
        return {"id": DEFAULT_ACCOUNT_ID, "name": "默认（DeepSeek）",
                "platform": "deepseek",
                "url": "https://api.deepseek.com/user/balance",
                "key": key, "inherited": True}

    def _all_accounts():
        acc = _saved_accounts()
        if acc:
            return acc
        d = _default_account()
        return [d] if d else []

    def _find(acc_id):
        for a in _all_accounts():
            if a.get("id") == acc_id:
                return a
        return None

    def _public(a):
        """给前端看的账户形态：Key 只给打码串，绝不下发明文。"""
        return {"id": a.get("id"), "name": a.get("name") or "未命名",
                "platform": a.get("platform") or "custom",
                "url": a.get("url") or "",
                "keyMask": _mask(a.get("key")), "hasKey": bool(a.get("key")),
                "inherited": bool(a.get("inherited"))}

    @api.route("GET", "/api/whale-widget/accounts")
    def accounts():
        return {"ok": True, "accounts": [_public(a) for a in _all_accounts()],
                "platforms": PLATFORMS}

    @api.route("POST", "/api/whale-widget/accounts")
    async def accounts_save(request: Request):
        """action=save 新增/更新一个账户；action=delete 删除一个账户。"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "error": "请求格式不对"}
        action = body.get("action")

        acc = _saved_accounts()

        if action == "delete":
            aid = body.get("id")
            left = [a for a in acc if a.get("id") != aid]
            store.set("accounts", left)
            # 顺手清掉它的缓存与记账，免得下次同 id 复用时串数据
            cache = store.get("balance_cache") or {}
            cache.pop(aid, None)
            store.set("balance_cache", cache)
            led = store.get("ledger") or {}
            led.pop(aid, None)
            store.set("ledger", led)
            return {"ok": True, "accounts": [_public(a) for a in (left or _all_accounts() or [])]}

        if action == "save":
            item = body.get("account") or {}
            aid = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip() or "未命名账户"
            url = str(item.get("url") or "").strip()
            platform = str(item.get("platform") or "custom").strip() or "custom"
            key_in = item.get("key")
            if platform != "custom" and not url:
                for p in PLATFORMS:
                    if p["id"] == platform:
                        url = p["url"]
                        break
            if not url:
                return {"ok": False, "error": "要填余额接口地址（多数平台没有公开接口，填不了就先别加）"}

            if aid and any(a.get("id") == aid for a in acc):
                # 更新：key 传空表示不改（避免「只改名字结果把 Key 清空」）
                for a in acc:
                    if a.get("id") == aid:
                        a["name"], a["url"], a["platform"] = name, url, platform
                        if key_in:
                            a["key"] = str(key_in).strip()
                        a.pop("inherited", None)
                store.set("accounts", acc)
            else:
                # id 用 uuid：早先用「秒×1000 取模」，同一秒内连加两个账户
                # 会生成同一个 id —— 查余额串到别人账上、删除还会连坐删掉两个。
                aid = aid or ("acc" + uuid.uuid4().hex[:10])
                if any(a.get("id") == aid for a in acc):
                    aid = "acc" + uuid.uuid4().hex[:10]
                key = str(key_in or "").strip()
                if not key:
                    return {"ok": False, "error": "新账户要填 API Key"}
                acc.append({"id": aid, "name": name, "platform": platform,
                            "url": url, "key": key})
                store.set("accounts", acc)
            return {"ok": True, "id": aid,
                    "accounts": [_public(a) for a in _all_accounts()]}

        return {"ok": False, "error": "不认识的动作：%s" % action}

    # ------------------------------------------------------------------ 余额
    def ledger_observe(acc_id, total):
        """记账模式：观测到余额下降就累加进今日已用；跨天归零。按账户分别记。"""
        today = _today_str()
        led = store.get("ledger") or {}
        lg = led.get(acc_id) or {}
        if lg.get("day") != today:
            lg = {"day": today, "prev": total, "usage": 0.0}
        prev = float(lg.get("prev", total))
        if total < prev - 1e-9:
            lg["usage"] = float(lg.get("usage", 0.0)) + (prev - total)
        lg["prev"] = total
        led[acc_id] = lg
        store.set("ledger", led)
        return float(lg.get("usage", 0.0))

    @api.route("GET", "/api/whale-widget/balance")
    def balance(account: str = ""):
        accs = _all_accounts()
        if not accs:
            return {"ok": False, "error": "还没配余额账户 —— 去「设置 → 小鲸鱼余额」加一个",
                    "accounts": [], "ts": _now_sec()}

        cur_acc = _find(account) if account else accs[0]
        if cur_acc is None:
            cur_acc = accs[0]
        acc_id = cur_acc.get("id")

        # 服务端缓存：TTL 内直接返回，避免每次刷新都打平台接口
        cache_all = store.get("balance_cache") or {}
        c = cache_all.get(acc_id) or {}
        if c.get("ok") and (_now_sec() - c.get("ts", 0)) < BALANCE_TTL:
            return c

        key = cur_acc.get("key") or ""
        url = cur_acc.get("url") or ""
        if not key:
            out = {"ok": False, "error": "这个账户还没填 API Key",
                   "account": _public(cur_acc), "ts": _now_sec()}
            cache_all[acc_id] = out
            store.set("balance_cache", cache_all)
            return out

        try:
            total, cur, extra = _parse(_fetch(url, key))
        except urllib.error.HTTPError as e:
            out = {"ok": False, "account": _public(cur_acc),
                   "error": "余额接口 HTTP %s %s" % (e.code, e.reason),
                   "ts": _now_sec()}
            cache_all[acc_id] = out
            store.set("balance_cache", cache_all)
            return out
        except urllib.error.URLError as e:
            out = {"ok": False, "account": _public(cur_acc),
                   "error": "连不上（网络/代理问题）：%s" % (e.reason,), "ts": _now_sec()}
            cache_all[acc_id] = out
            store.set("balance_cache", cache_all)
            return out
        except Exception as e:
            out = {"ok": False, "account": _public(cur_acc),
                   "error": "%s: %s" % (type(e).__name__, e), "ts": _now_sec()}
            cache_all[acc_id] = out
            store.set("balance_cache", cache_all)
            return out

        today_usage = ledger_observe(acc_id, total)
        out = {
            "ok": True,
            "balance": total,
            "currency": cur,
            "extra": extra,
            "todayUsage": today_usage,
            "isPeak": _is_peak_time(_now_sec()),
            "account": _public(cur_acc),
            "accounts": [_public(a) for a in accs],
            "ts": _now_sec(),
        }
        cache_all[acc_id] = out
        store.set("balance_cache", cache_all)
        return out
