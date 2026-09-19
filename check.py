# -*- coding: utf-8 -*-
"""
多 AI 工作台 · 启动自检
======================
把「过两天突然用不了」的常见死因一次性查清楚，并给出明确修复指引。

检查项：
  1. 依赖（fastapi / uvicorn）
  2. config.json（缺失不算错 —— 首次启动会自动生成一份空的）
  3. 每个 AI 成员的接口配置（有没有可用的地址 + Key）
  4. 接口连通性（逐个成员实测，可用 --quick 跳过）
  5. 端口是否被占用
  6. 运行目录（共享记忆 / 聊天记录 / 工作区）

注意：只有「缺依赖 / config.json 损坏 / 目录不可写」才算致命。
      某个 AI 的 Key 失效、模型名写错、接口不通，一律只记警告 ——
      不影响程序启动，你照常打开工作台，进去改就行。

用法：
  python check.py          完整检查（含联网，约 3-8 秒）
  python check.py --quick  跳过联网检查
退出码：0 = 可以启动，1 = 有致命问题
"""
import json
import os
import socket
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OK, WARN, BAD = "[ OK ]", "[警告]", "[致命]"
fatal = 0
warns = 0


def say(tag, msg):
    print(f"{tag} {msg}")


def mark_bad(msg):
    global fatal
    fatal += 1
    say(BAD, msg)


def mark_warn(msg):
    global warns
    warns += 1
    say(WARN, msg)


def _resolve(p, default):
    """把配置里的目录（可能相对）解析成本机绝对路径"""
    p = str(p or default)
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(BASE, p)
    return os.path.normpath(p)


print("=" * 56)
print(" 多 AI 工作台 · 启动自检")
print("=" * 56)

# ---------- 1. 依赖 ----------
try:
    import fastapi
    import uvicorn
    say(OK, f"依赖齐全：fastapi {fastapi.__version__} / uvicorn {uvicorn.__version__}")
except Exception as e:
    mark_bad(f"缺少依赖（{type(e).__name__}: {e}）。"
             f"修复：python -m pip install -r requirements.txt")
    fastapi = uvicorn = None

# ---------- 2. 配置 ----------
cfg_path = os.path.join(BASE, "config.json")
cfg = {}
if not os.path.isfile(cfg_path):
    mark_warn("还没有 config.json —— 首次启动会自动生成一份空的，"
              "启动后在网页里「＋ 添加第一个 AI」即可。")
else:
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        say(OK, "config.json 可读")
    except Exception as e:
        mark_bad(f"config.json 解析失败：{e}（改坏了就把它删掉，会重新生成一份空的）")
        cfg = {}

# ---------- 3. 成员接口配置 ----------
default_api = cfg.get("deepseek") or {}
agents = cfg.get("agents") or []
first_ready = None
probes = []          # 所有「有 Key」的成员，逐个做连通性实测


def _mask(k):
    k = str(k or "")
    return f"{k[:6]}…{k[-4:]}" if len(k) > 12 else "已填"


if not agents:
    mark_warn("当前还没有任何 AI 成员。启动后到「设置 → AI 成员与模型」添加一个。")
else:
    say(OK, f"共 {len(agents)} 个成员")
    n_ready = 0
    for a in agents:
        if not isinstance(a, dict):
            continue
        name = a.get("name") or a.get("key") or "?"
        p = a.get("provider") or {}
        key = str(p.get("api_key") or "").strip() or str(default_api.get("api_key") or "").strip()
        base = str(p.get("base_url") or "").strip() or str(default_api.get("base_url") or "").strip()
        model = (str(p.get("model") or "").strip()
                 or str(a.get("model") or "").strip()
                 or str(default_api.get("model") or "").strip())
        if not key:
            mark_warn(f"成员「{name}」还没有可用的 API Key（它自己没配，默认接口也是空的）。"
                      f"去「设置 → AI 成员与模型」点它那行的「编辑」填上。")
        else:
            n_ready += 1
            say(OK, f"成员「{name}」接口就绪"
                    f"（{base or '默认地址'} / {model or '默认模型'} / Key {_mask(key)}）")
            probes.append((name, base or "https://api.deepseek.com/v1",
                           key, model or "deepseek-flash"))
    if n_ready:
        say(OK, f"{n_ready}/{len(agents)} 个成员可直接对话")

# ---------- 4. 接口连通性（逐个成员实测；出错只警告，不阻断启动） ----------
if "--quick" in sys.argv:
    say(OK, "已按 --quick 跳过联网检查")
elif not probes:
    say(OK, "没有可用的成员接口，跳过联网检查")
else:
    for name, base, key, model in probes:
        url = base.rstrip("/") + "/chat/completions"
        payload = json.dumps({"model": model,
                              "messages": [{"role": "user", "content": "ping"}],
                              "stream": False, "max_tokens": 5}).encode()
        req = urllib.request.Request(url, data=payload, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
            say(OK, f"成员「{name}」实测连通（返回模型 {d.get('model', model)}）")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if e.code == 401:
                hint = "API Key 无效或已失效。到设置里给它换一个 Key。"
            elif e.code == 402:
                hint = "余额不足，去对应平台充值。"
            elif e.code in (400, 404):
                hint = ("模型名或地址不对。模型名必须填服务商的真实 ID，"
                        "不能自己写简称（例：Moonshot 是 kimi-k2.6，DeepSeek 是 deepseek-flash）；"
                        "base_url 一般要写到 /v1 这一层。")
            else:
                hint = "核对 Key / 地址 / 模型名。"
            mark_warn(f"成员「{name}」接口返回 HTTP {e.code}：{hint}")
            if body:
                print(f"         服务端原话：{body}")
        except Exception as e:
            mark_warn(f"成员「{name}」连不上（{type(e).__name__}: {e}）。"
                      f"可能是断网或系统代理问题；该成员不会回话，但不影响程序启动。")

# ---------- 5. 端口 ----------
port = cfg.get("port", 7860)
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    busy = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
except Exception:
    busy = False
if busy:
    mark_warn(f"端口 {port} 已被占用 —— 可能工作台已经在跑了（那就直接用），"
              f"或被别的程序占了（改 config.json 的 port）")
else:
    say(OK, f"端口 {port} 空闲")

# ---------- 6. 运行目录 ----------
for label, key, dflt in (("共享记忆", "shared_memory", "./shared"),
                         ("聊天记录", "sessions_dir", "./sessions"),
                         ("工作区", "workspaces_dir", "./workspaces")):
    p = _resolve(cfg.get(key), dflt)
    if os.path.isdir(p):
        say(OK, f"{label}目录就绪：{p}")
    else:
        try:
            os.makedirs(p, exist_ok=True)
            say(OK, f"{label}目录缺失，已自动创建：{p}")
        except Exception as e:
            mark_bad(f"{label}目录创建失败：{p}（{e}）")

# ---------- 结论 ----------
print("=" * 56)
if fatal:
    print(f"结论：发现 {fatal} 个致命问题（缺依赖 / config.json 损坏 / 目录不可写），")
    print(f"      程序无法正常启动。请按上面提示修复后重试。")
    sys.exit(1)
if warns:
    print(f"结论：可以启动。有 {warns} 条警告 —— 标 [警告] 的成员可能不回话，")
    print(f"      但不影响程序打开，进去到「设置 → AI 成员与模型」里改就行。")
else:
    print("结论：可以启动，全部检查通过。")
sys.exit(0)
