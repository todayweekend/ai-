# -*- coding: utf-8 -*-
"""通用工作引擎 + 分身配方 —— 端到端测试。

先启动工作台服务，再跑：
    python tests/test_engine.py
可用环境变量 WB_BASE 指定地址（默认 http://127.0.0.1:7860）。

覆盖：引擎探测 / 命令引擎（参数传法、stdin 传法）/ 超时降级 / 找不到命令降级 /
      切回接口引擎 / engine 字段回填一致性 / 导入身份文件建成员 / 收尾清理。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("WB_BASE", "http://127.0.0.1:7860")
OUT, FAIL = [], []
PY = sys.executable or "python"
SID = "eng_test_%d" % int(time.time() % 100000)

# 绕开系统代理：本机 127.0.0.1 有时会被本机代理劫持成 502
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(s=""):
    OUT.append(str(s))


def ok(name, cond, extra=""):
    if not cond:
        FAIL.append(name)
    tail = "" if (cond or not extra) else ("  <- " + str(extra)[:220])
    log("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, tail))


def get(path):
    try:
        with _OPENER.open(BASE + path, timeout=120) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def post(path, obj, timeout=300):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def chat(ai, msg, timeout=300):
    """走单聊 SSE，把 token 拼起来返回。"""
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({"ai": ai, "session_id": SID, "message": msg}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    parts = []
    with _OPENER.open(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            if ev.get("type") == "token":
                parts.append(ev.get("text", ""))
    return "".join(parts)


def set_engine(cmd=None, timeout=None, cwd=None, kind="command"):
    """给测试成员配引擎"""
    eng = {"type": kind}
    if kind == "command":
        eng["cmd"] = cmd or ""
        if timeout is not None:
            eng["timeout"] = timeout
        if cwd:
            eng["cwd"] = cwd
    return post("/api/agents", {"action": "update", "key": KEY, "engine": eng})


def find_agent(key):
    _, txt = get("/api/agents")
    for a in json.loads(txt).get("agents", []):
        if a.get("key") == key:
            return a
    return None


KEY = "eng_probe"
PKEY = "persona_probe"

# 跑之前先给成员列表拍个快照：收尾时把「新增的」全删掉，
# 这样不管中间建了几个临时成员（包括 import-personas 自动生成 key 的那个）都不会漏。
_, _t0 = get("/api/agents")
BASE_KEYS = {a.get("key") for a in json.loads(_t0).get("agents", [])}

log("== 1. 引擎探测 GET /api/agents/detect ==")
st, txt = get("/api/agents/detect")
d = json.loads(txt)
ok("接口 200", st == 200, st)
ids = [f["id"] for f in d.get("found", [])]
ok("至少探测到一条（python 示例兜底）", len(ids) >= 1, ids)
ok("带 python 示例引擎", "python-demo" in ids, ids)
demo = [f for f in d.get("found", []) if f["id"] == "python-demo"]
ok("示例命令非空", bool(demo and demo[0].get("cmd")), demo)

log("")
log("== 2. 建测试成员（外部命令引擎·参数传法） ==")
post("/api/agents", {"action": "delete", "key": KEY})
st, txt = post("/api/agents", {"action": "add", "key": KEY, "name": "引擎探针",
                               "emoji": "⚙", "personas": ""})
ok("成员创建成功", st == 200 and json.loads(txt).get("ok"), txt[:160])
time.sleep(0.3)
a = find_agent(KEY)
ok("新成员默认是接口引擎", a and (a.get("engine") or {}).get("type") == "api",
   a and a.get("engine"))

cmd_arg = '"%s" -c "import sys;print(\'ARG:\'+sys.argv[1])" "{prompt}"' % PY
set_engine(cmd=cmd_arg)
time.sleep(0.2)
a = find_agent(KEY)
ok("引擎已存成 command", a and (a.get("engine") or {}).get("type") == "command",
   a and a.get("engine"))
out = chat(KEY, "hello-engine")
ok("命令引擎答话（参数传法）", "ARG:" in out, out[:200])

log("")
log("== 3. stdin 传法（命令里不写 {prompt}） ==")
cmd_stdin = '"%s" -c "import sys;print(\'STDIN:\'+sys.stdin.read().strip())"' % PY
set_engine(cmd=cmd_stdin)
time.sleep(0.2)
a = find_agent(KEY)
ok("回填时标出走的 stdin", bool((a.get("engine") or {}).get("stdin")), a and a.get("engine"))
out = chat(KEY, "走标准输入")
ok("命令引擎答话（stdin 传法）", "STDIN:" in out, out[:200])

log("")
log("== 4. 超时降级 ==")
set_engine(cmd='"%s" -c "import time;time.sleep(4)"' % PY, timeout=1)
time.sleep(0.2)
t0 = time.time()
out = chat(KEY, "超时测试")
ok("超时被拦下并降级", "超时" in out, out[:200])
ok("确实按时返回（<3s）", (time.time() - t0) < 3.0, "%.1fs" % (time.time() - t0))

log("")
log("== 5. 找不到命令降级 ==")
set_engine(cmd="definitely_not_a_command_xyz_12345 {prompt}")
time.sleep(0.2)
out = chat(KEY, "找不到命令测试")
ok("给出可读的错误而不是崩掉", "找不到外部命令" in out, out[:200])

log("")
log("== 6. engine 字段回填一致性（引号不能丢） ==")
set_engine(cmd=cmd_arg)
time.sleep(0.2)
a = find_agent(KEY)
back = (a.get("engine") or {}).get("cmd") or ""
ok("回填含 {prompt}", "{prompt}" in back, back)
ok("回填含引号", '"' in back, back)
out = chat(KEY, "roundtrip")
ok("回填后再答一次仍然正常", "ARG:" in out, out[:200])

log("")
log("== 7. 切回接口引擎 ==")
set_engine(kind="api")
time.sleep(0.2)
a = find_agent(KEY)
ok("engine 字段已清掉（回到接口引擎）",
   (a.get("engine") or {}).get("type") == "api", a and a.get("engine"))

log("")
log("== 8. 分身配方：导入身份文件建成员 ==")
post("/api/agents", {"action": "delete", "key": PKEY})
files = [
    {"name": "IDENTITY.md", "content": "# 试验分身\n\n- **Name:** 试验者\n- **Vibe:** 简洁\n"},
    {"name": "SOUL.md", "content": "# 灵魂\n\n直说，不客套。\n"},
]
st, txt = post("/api/agents/import-personas", {"files": files, "name": ""})
r = json.loads(txt)
ok("导入成功", st == 200 and r.get("ok"), txt[:200])
ok("自动认出了名字（从 IDENTITY.md）", r.get("name") == "试验者", r.get("name"))
saved = r.get("files") or []
ok("两份文件都落盘", len(saved) == 2 and all(os.path.isfile(p) for p in saved), saved)
pa = find_agent(r.get("key"))
ok("成员的人设文件指过去了", pa and len(pa.get("persona_files") or []) == 2,
   pa and pa.get("persona_files"))
ok("新成员默认接口引擎（未配 key → 未就绪）",
   (pa.get("engine") or {}).get("type") == "api", pa and pa.get("engine"))

log("")
log("== 9. 收尾：删掉本次跑出来的所有成员 ==")
import shutil
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 先删成员 —— 必须走在清目录前面。反过来的话，成员还在，
# 下一次 _reload_agents() 会照着 workspace 字段把 ws_ 目录又建回来。
_, _t1 = get("/api/agents")
NEW_KEYS = sorted({a.get("key") for a in json.loads(_t1).get("agents", [])} - BASE_KEYS)
for k in NEW_KEYS:
    post("/api/agents", {"action": "delete", "key": k})
time.sleep(0.3)

left_members = sorted({a.get("key") for a in json.loads(get("/api/agents")[1]).get("agents", [])}
                      - BASE_KEYS)
ok("本次新建的成员已全部清除", not left_members, left_members)

# 再清人设目录 / 工作区（先删成员，目录才不会被重建）。
# 除了本次 NEW_KEYS，还要扫磁盘上残留的 personas/<k>/ 和 ws_<k>/，
# 把「不属于基线成员」的孤儿目录一并删掉（防止历史 run 漏下的目录越积越多）。
cand = set(NEW_KEYS)
for k in (KEY, PKEY, str(r.get("key") or "")):
    if k:
        cand.add(k)
pdir = os.path.join(ROOT, "personas")
if os.path.isdir(pdir):
    for sub in os.listdir(pdir):
        if os.path.isdir(os.path.join(pdir, sub)):
            cand.add(sub)
for sub in os.listdir(ROOT):
    if sub.startswith("ws_") and os.path.isdir(os.path.join(ROOT, sub)):
        cand.add(sub[3:])
cand -= BASE_KEYS


def _rm_retry(d, tries=3):
    """删目录，带重试（Windows 偶尔有文件锁），删后校验。"""
    for _ in range(tries):
        try:
            if os.path.isdir(d):
                shutil.rmtree(d)
        except Exception:
            time.sleep(0.2)
    return not os.path.isdir(d)


bad = []
for k in sorted(cand):
    for d in (os.path.join(ROOT, "personas", k), os.path.join(ROOT, "ws_" + k)):
        if os.path.isdir(d) and not _rm_retry(d):
            bad.append(d)
ok("人设目录与工作区已清理", not bad, bad)

log("")
log("=== 结果：%d 项失败 ===" % len(FAIL))
if FAIL:
    log("失败项：" + "；".join(FAIL))

rep = os.path.join(os.environ.get("TEMP", "."), "wb_engine_report.md")
try:
    with open(os.path.abspath(rep), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    print("\n".join(OUT))
except Exception as e:
    print("\n".join(OUT))
    print("（报告写入失败：%r）" % (e,))
sys.exit(1 if FAIL else 0)
