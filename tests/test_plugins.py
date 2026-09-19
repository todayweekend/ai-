# -*- coding: utf-8 -*-
"""
插件系统端到端测试
==================
对着**正在运行**的工作台打一遍接口，逐层校验插件系统：
清单 / UI 资源 / 静态素材 / command 钩子 / before_send / 自带接口 / 私有存储 /
前端注入器 / 实时事件通道 / 开关插件后界面资源的增删。

用法（先启动服务）：
    venv\\Scripts\\python.exe tests\\test_plugins.py
退出码 0 = 全过，1 = 有失败项。
"""
import json
import os
import sys
import time
import urllib.request

# Windows 控制台默认不是 UTF-8，中文输出会乱码 → 这里统一成 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.environ.get("WB_BASE", "http://127.0.0.1:7860")
PLUGIN = "example-skin"          # 仓库自带的示例插件

# 本机代理（Clash 等）会连 127.0.0.1 一起拦 → 测试自己绕开代理
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

FAIL = []


def has_plugin_ui(ui, kind, rel):
    """ui 里是否含【本插件】的某个资源。

    不能用全等比较：工作区里可能还有别的注入型插件（自带 whale-widget 默认就是启用的），
    全等会把它们的资源也算进来，导致测试随工作区状态漂移。
    """
    return f"/ext/{PLUGIN}/{rel}" in (ui.get(kind) or [])


def ok(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + ("  " + str(extra) if extra else ""))
    if not cond:
        FAIL.append(name)


def section(t):
    print("\n== " + t + " ==")


def get(path, timeout=20):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def post(path, body, timeout=30):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def sse(path, body, timeout=60, limit=400):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    out = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                try:
                    out.append(json.loads(line[5:].strip()))
                except Exception:
                    pass
            if len(out) >= limit:
                break
    return out


AI = None          # 当前工作台的第一个成员（没有成员时为 None）


def first_agent():
    """取第一个成员：测试借它发一条真实消息，来验证 command / before_send 钩子"""
    try:
        ags = (json.loads(get("/api/agents")[1]) or {}).get("agents") or []
        return ags[0]["key"] if ags else None
    except Exception:
        return None


def main():
    global AI
    AI = first_agent()
    section("0. 复位示例插件（保证测的是初始状态）")
    post("/api/plugins/toggle", {"name": PLUGIN, "enabled": False})
    time.sleep(0.5)
    d = json.loads(get("/api/plugins")[1])
    mine = [p for p in d.get("plugins", []) if p["name"] == PLUGIN]
    ok(f"{PLUGIN} 存在且已复位为停用", bool(mine) and mine[0]["enabled"] is False)

    section("1. 插件清单 /api/plugins")
    st, txt = get("/api/plugins")
    d = json.loads(txt)
    ok("接口 200", st == 200)
    ok("钩子清单 11 个", len(d.get("hook_names", [])) == 11, d.get("hook_names"))
    ok("插槽清单非空", "settings" in d.get("ui_slots", []), d.get("ui_slots"))
    skin = [p for p in d.get("plugins", []) if p["name"] == PLUGIN][0]
    ok("清单带 ui 段统计", bool(skin.get("ui")), skin.get("ui"))
    ok("默认停用", skin["enabled"] is False)

    section("2. 界面资源 /api/plugins/ui")
    ui = json.loads(get("/api/plugins/ui")[1])
    ok("停用时本插件界面资源未注入",
       not has_plugin_ui(ui, "css", "ui.css") and not has_plugin_ui(ui, "js", "ui.js")
       and not has_plugin_ui(ui, "background", "bg.svg"), ui)
    post("/api/plugins/toggle", {"name": PLUGIN, "enabled": True})
    time.sleep(0.6)
    ui = json.loads(get("/api/plugins/ui")[1])
    ok("注入 CSS", has_plugin_ui(ui, "css", "ui.css"), ui.get("css"))
    ok("注入 JS", has_plugin_ui(ui, "js", "ui.js"), ui.get("js"))
    ok("背景板就位", (ui.get("background") or {}).get("url") == f"/ext/{PLUGIN}/bg.svg",
       ui.get("background"))
    ok("body 类名注入", PLUGIN in (ui.get("bodyClass") or []), ui.get("bodyClass"))
    ok("设置插槽片段注册",
       ui.get("snippets", {}).get("settings") == [f"/ext/{PLUGIN}/snippets/settings.html"],
       ui.get("snippets"))

    section("3. 插件静态素材（任意文件都能被 /ext 提供）")
    for path, tag in ((f"/ext/{PLUGIN}/bg.svg", "背景板"),
                      (f"/ext/{PLUGIN}/snippets/settings.html", "插槽片段"),
                      (f"/ext/{PLUGIN}/panel.html", "面板页")):
        try:
            st, body = get(path)
            ok(f"{tag}可访问", st == 200 and len(body) > 50, f"{len(body)} 字节")
        except Exception as e:
            ok(f"{tag}可访问", False, repr(e))

    section("4. command 钩子（/指令由插件作答，不花模型额度）")
    if not AI:
        print("  [SKIP] 工作台里还没有成员，跳过「发一条真实消息」这组用例。")
        print("         （先去界面加一个成员，本节才有意义）")
    else:
        evs = sse("/api/chat", {"ai": AI, "session_id": "_plugin_test",
                                "message": "/skin"})
        text = "".join(e.get("text", "") for e in evs if e.get("type") == "token")
        ok("有回复", bool(text.strip()), text[:60])
        ok("回复来自插件", "星轨夜空皮肤在" in text)
        ok("before_send 生效（带签名）", "✦ 星轨" in text)

    section("5. 插件自带接口 + 私有存储")
    d = json.loads(get(f"/api/{PLUGIN}/stats")[1])
    ok("接口可用", d.get("ok") is True, d)
    if AI:
        ok("store 记录了回复次数", d.get("replies", 0) >= 1, d.get("replies"))
    else:
        print("  [SKIP] 第 4 节已跳过、没有真实回复，计数校验一并跳过")
    ok("接口可写 store",
       json.loads(post(f"/api/{PLUGIN}/sign", {"sign": False})[1]).get("sign") is False)
    post(f"/api/{PLUGIN}/sign", {"sign": True})

    section("6. 前端注入器（index.html 里该有的都在）")
    st, html = get("/")
    ok("index.html 可访问", st == 200)
    for key in ('id="pluginBg"', "window.WB", "loadPluginUI", "connectPluginStream",
                "wbSlotHost", "wbClearInjected", "ev.type==='replace'",
                "addRailButton", "addSettingSection"):
        ok("前端含 " + key, key in html)

    section("7. 实时事件通道 /api/plugins/stream")
    try:
        with urllib.request.urlopen(BASE + "/api/plugins/stream", timeout=8) as r:
            first = r.readline().decode("utf-8", "replace").strip()
        ok("首包是 hello 握手", "hello" in first, first[:70])
    except Exception as e:
        ok("首包是 hello 握手", False, repr(e))

    section("8. 复原（示例插件改回停用）")
    post("/api/plugins/toggle", {"name": PLUGIN, "enabled": False})
    time.sleep(0.4)
    ok("停用后本插件界面注入清空",
       not has_plugin_ui(json.loads(get("/api/plugins/ui")[1]), "css", "ui.css"))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        FAIL.append("异常中断：" + repr(e))
    print("\n=== %d 项失败 ===" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1 if FAIL else 0)
