# -*- coding: utf-8 -*-
"""
多 AI 工作台 · 后端
===================
N 个 AI 成员在同一个网页里协作（成员名单在 config.json 的 agents 数组，可自由增删）：
  - 单聊：任意一个 AI
  - 同时问：同一问题发给多个 AI，N 栏对比
  - 群聊：自选哪几个 AI 同场混着说；每个成员可设「组长 / 平权 / 旁听」
  - 让它们聊：自选成员与发言顺序，轮流发言

每个 AI 都是「带工具」的真身 agent：能读/写文件、跑命令、查写共享记忆。
共享记忆区：config.json 的 shared_memory（默认与本程序同级的 shared/）。
【共享记忆铁律】每个 AI 只写自己前缀（agents 里各自 prefix 字段）的文件，
不创建、不覆盖别人前缀的文件。

依赖：fastapi / uvicorn / httpx（用 pip 装到本程序自带 venv，见底部说明）
"""

import json
import os
import importlib.util
import re
import sys
import time
import queue
import asyncio
import datetime
import urllib.request
import urllib.error
import uuid
import threading
import concurrent.futures
import shutil
import subprocess

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# 0. 加载配置
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CONFIG_EXAMPLE_PATH = os.path.join(BASE_DIR, "config.example.json")

# --- 后台静默运行兼容（pythonw / vbs 隐藏拉起）---------------------------------
# 这类启动方式没有控制台，sys.stdout / sys.stderr 都是 None，后果有两个：
#   1) print 会抛异常 —— 底部 uvicorn.run() 前正好有一句，进程直接退出；
#   2) uvicorn 自己的日志初始化会调 sys.stdout.isatty()
#      （uvicorn/logging.py: self.use_colors = sys.stdout.isatty()），
#      同样抛 AttributeError。
# 所以只遮蔽 print 不够，必须给 stdout / stderr 装一个「像流一样」的对象，
# 并把内容落到 logs/server.log —— 排错有据可查，服务照常起来。
class _NullLogStream:
    """无控制台时的兜底标准流：可写、可 flush、isatty() 返回 False。"""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, fh=None):
        self._fh = fh

    def write(self, s):
        try:
            if self._fh is not None:
                self._fh.write(s)
                self._fh.flush()
        except Exception:
            pass
        return len(s) if isinstance(s, str) else 0

    def flush(self):
        try:
            if self._fh is not None:
                self._fh.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    def fileno(self):
        raise OSError("no fileno")

    def close(self):
        pass


def _install_null_streams():
    """无控制台（pythonw 等）时，给 sys.stdout / sys.stderr 装兜底流。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    _fh = None
    try:
        _log_dir = os.path.join(BASE_DIR, "logs")
        os.makedirs(_log_dir, exist_ok=True)
        _fh = open(os.path.join(_log_dir, "server.log"), "a", encoding="utf-8", buffering=1)
    except Exception:
        _fh = None
    if sys.stdout is None:
        sys.stdout = _NullLogStream(_fh)
    if sys.stderr is None:
        sys.stderr = _NullLogStream(_fh)


_install_null_streams()

# 内置兜底配置 = 一份「没有成员、没有密钥」的空配置。
# 分享给别人时，对方第一次启动走这条路径：拿到空工作台，自己填接口和 Key，
# 不会用上任何人的密钥。
FALLBACK_CONFIG = {
    "deepseek": {"api_key": "", "model": "deepseek-flash",
                 "base_url": "https://api.deepseek.com/v1"},
    "agents": [],
    "shared_memory": os.path.join(BASE_DIR, "shared"),
    "sessions_dir": os.path.join(BASE_DIR, "sessions"),
    "port": 7860,
    "max_tool_rounds": 8,
}


def _load_config():
    """读 config.json；不存在则用 config.example.json（其次内置模板）生成一份空的。"""
    made = False
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = None
        if os.path.isfile(CONFIG_EXAMPLE_PATH):
            try:
                with open(CONFIG_EXAMPLE_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = None
        cfg = cfg if isinstance(cfg, dict) else {}
        # 剔除模板里的「注释键」（以 // 开头，仅给人看，程序不读），避免生成空配置时
        # 把示例成员的占位 "sk-…" 等无用内容也写进用户的 config.json
        for k in [k for k in list(cfg) if isinstance(k, str) and k.startswith("//")]:
            cfg.pop(k, None)
        # 路径类默认值按本机实际算，不能照抄模板里的绝对路径
        for k in ("shared_memory", "sessions_dir"):
            cfg.pop(k, None)
        for k, v in FALLBACK_CONFIG.items():
            cfg.setdefault(k, v)
        made = True
    cfg.setdefault("agents", [])
    if not isinstance(cfg.get("agents"), list):
        cfg["agents"] = []
    ds = cfg.setdefault("deepseek", {})
    if not isinstance(ds, dict):
        ds = {}
        cfg["deepseek"] = ds
    for k, v in FALLBACK_CONFIG["deepseek"].items():
        ds.setdefault(k, v)
    for k in ("shared_memory", "sessions_dir", "port", "max_tool_rounds"):
        cfg.setdefault(k, FALLBACK_CONFIG[k])
    if made:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            print("[配置] 首次启动：已生成空的 config.json（请填入你自己的接口与 API Key）")
        except Exception as e:
            print("[配置] 写入 config.json 失败：", e)
    return cfg


CONFIG = _load_config()
DS = CONFIG["deepseek"]

DEFAULT_COLORS = ["#2563eb", "#0d9488", "#6d28d9", "#b45309", "#be185d", "#059669"]

def _load_agents():
    """
    从 config 读 AI 成员注册表：
      · 新配置：config["agents"] = [ {key,name,emoji,color,prefix,workspace,persona_files,model}, ... ]
      · 旧配置（早期两段式写法）自动转换，向后兼容
    """
    ags = CONFIG.get("agents")
    if not ags:
        ags = []
        for key in ("agent_a", "agent_b"):
            if key in CONFIG:
                c = dict(CONFIG[key]); c.setdefault("key", key); ags.append(c)
    out = {}
    for i, c in enumerate(ags):
        key = (c.get("key") or f"ai{i+1}").strip()
        c["key"] = key
        c.setdefault("name", key)
        c.setdefault("emoji", "🤖")
        c.setdefault("color", DEFAULT_COLORS[i % len(DEFAULT_COLORS)])
        c.setdefault("prefix", key[:2].upper() + "_")
        c.setdefault("workspace", os.path.join(BASE_DIR, "ws_" + key))
        c.setdefault("persona_files", [])
        c.setdefault("model", "deepseek-flash")
        # trusted：允许它改动本程序代码（默认只对第一个成员开启）
        c.setdefault("trusted", key == "agent_a")
        out[key] = c
    return out

AI_CONF = _load_agents()
AGENT_ORDER = list(AI_CONF.keys())

SHARED_MEMORY = os.path.normpath(CONFIG["shared_memory"])
SESSIONS_DIR = os.path.normpath(CONFIG["sessions_dir"])
PORT = CONFIG.get("port", 7860)
MAX_TOOL_ROUNDS = CONFIG.get("max_tool_rounds", 8)

# ---------------------------------------------------------------------------
# 0a. 可选模型目录（含官方价格，单位：元 / 百万 tokens）
#     价格以 DeepSeek 官方公布为准；缓存命中价是“重复上下文”的优惠价（仅供参考）。
# ---------------------------------------------------------------------------
# 模型目录：以官方 API /models 实测 + 官方定价页为准（2026-09-13 核对）
# 注意：旧名 deepseek-chat/deepseek-reasoner 是上代模型，已由官方映射到新模型，不再单列。
# 价格单位：元/百万tokens，取【高峰时段】价；空闲时段一律减半。
MODEL_CATALOG = {
    "deepseek-flash": {
        "id": "deepseek-flash",
        "label": "DeepSeek-V4.1-Flash（推荐·快）",
        "price_in": 2.0, "price_out": 8.0, "cache_in": 0.04,
        "speed": "快", "think": True,
        "desc": "当前主力：1M 上下文、默认思考模式、支持图像理解；缓存命中输入 0.04 元，空闲时段全部减半",
    },
    "deepseek-v4-pro": {
        "id": "deepseek-v4-pro",
        "label": "DeepSeek-V4-Pro（旧旗舰·将下线）",
        "price_in": 9.0, "price_out": 27.0, "cache_in": 0.30,
        "speed": "中", "think": True,
        "desc": "官方已宣布 V4.1 Flash 全面超越它；9-14 12:00 起其请求自动路由到 Flash 并按 Flash 价格计费",
    },
}

# ---------------------------------------------------------------------------
# 0b. 用户设置（外观 / 文字 / 会话 / 外部引擎插件）—— 存 settings.json，与 config.json 分开
# ---------------------------------------------------------------------------
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
DEFAULT_SETTINGS = {
    "theme": "light",          # light / dark
    "round": "compact",        # compact / soft
    "accent": "#2563eb",       # 强调色
    "fontSize": 14,            # 正文字号 px
    "lineHeight": 1.6,
    "msgWidth": 78,            # 气泡最大宽度 %
    "gradAlpha": 0.28,         # 气泡左侧渐变浓度
    "gradWidth": 25,           # 渐变蔓延宽度 %
    "showBar": True,           # 是否显示左侧 3px 色条
    "showHead": True,          # 是否显示气泡头部（色点+名字）
    "showTools": True,         # 是否显示工具调用
    "toolsOpen": False,        # 工具结果是否默认展开
    "autoScroll": True,
    "duetTurns": 4,            # 互聊轮次
    "defaultMode": "group",    # 打开时默认进入的模式
    "replyLength": "concise",  # concise / normal / detailed
    "engineMode": "api",      # api=内置分身(快/可流式)  harness=外部引擎真身(慢/是真本体)
    "groupDecide": True,       # 群聊：True=先判断谁该接话(首字略慢/不抢话) False=两个都回(首字更快/可能重复)
    "showThink": True,         # 是否显示 AI 的思考过程
    "agent_a_model": "deepseek-flash",  # 各 AI 的模型（新成员通用规则：{key}_model）
    "agent_b_model": "deepseek-flash",    # 该成员使用的模型（仅 api 分身模式生效；harness 真身用其自身配置）
    "maxOutputTokens": 8000,            # 单次回复/工具参数输出上限（tokens）；抬高可避免大文件写入被截断导致工具参数残缺
}
SETTINGS = dict(DEFAULT_SETTINGS)
if os.path.isfile(SETTINGS_PATH):
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            SETTINGS.update(json.load(f))
    except Exception:
        pass

def get_model(ai_key: str):
    """返回某个 AI 当前应选用的模型：
    外部协议成员（自带 provider.model）直接用它的模型名，不经 DeepSeek 目录过滤；
    DeepSeek 成员走 设置里的 {key}_model → agent 自带 model → 全局默认"""
    ag = AI_CONF.get(ai_key, {})
    p = ag.get("provider") or {}
    if p.get("model"):
        return p["model"]
    m = SETTINGS.get(f"{ai_key}_model") or ag.get("model") or DS.get("model", "deepseek-flash")
    if m not in MODEL_CATALOG:
        m = DS.get("model", "deepseek-flash")
    # 旧模型名是上代遗留，直接映射到现行模型
    if m == "deepseek-chat" or m == "deepseek-reasoner":
        m = "deepseek-flash"
    return m

def _prov(ai_key: str):
    """该 AI 的接口提供方（OpenAI 兼容协议：GPT / Kimi / GLM / 通义 / DeepSeek 都长这样）。
    agent 自带 provider（config.json 里 provider.base_url/api_key/model）→ 否则回退全局 DeepSeek 配置。
    这是"接任意平台 AI"的协议层：新平台只要兼容 OpenAI chat/completions 就能插进来。"""
    ag = AI_CONF.get(ai_key, {})
    p = ag.get("provider") or {}
    return {
        "base_url": _norm_base(p.get("base_url") or DS["base_url"]),
        "api_key":  p.get("api_key")  or DS["api_key"],
        "model":    p.get("model")    or DS["model"],
    }


def _prov_ready(ai_key: str) -> bool:
    """这个成员现在能不能真的被调用 —— 有 API Key 才算配置好了。"""
    try:
        return bool((_prov(ai_key) or {}).get("api_key"))
    except Exception:
        return False


def _no_key_msg(ai_key: str) -> str:
    ai = AI_CONF.get(ai_key, {})
    return ("[接口未配置] 成员「%s」还没有可用的 API Key，暂时无法回答。\n"
            "请打开：设置 → AI 成员与模型 → 点它这行的「编辑」→ 填入"
            "接口地址 / API Key / 模型名，保存后立刻生效。" % ai.get("name", ai_key))


def save_settings(new_vals: dict):
    global SETTINGS
    SETTINGS.update(new_vals or {})
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, ensure_ascii=False, indent=2)
    # 设置可能改变系统提示（如回复长度）→ 重建
    rebuild_prompts()
    return SETTINGS

# 外部引擎（dsh）插件库位置
# DSH home：优先环境变量，其次 config.json 的 dsh_home，最后 ~/.dsh
DSH_HOME_DIR = os.path.normpath(
    os.environ.get("DSH_HOME")
    or CONFIG.get("dsh_home")
    or os.path.join(os.path.expanduser("~"), ".dsh"))
DSH_WEB_PROFILE = os.path.normpath(CONFIG.get(
    "dsh_web_profile", os.path.join(DSH_HOME_DIR, "profiles", "web", "package.json")))
ENGINE_PLUGINS_DIR = os.path.normpath(CONFIG.get(
    "engine_plugins_dir", os.path.join(BASE_DIR, "engine_plugins")))
DSH_BUILTIN_BUNDLES = {"@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"}

REPLY_LEN_HINT = {
    "concise": "回复尽量简洁：直给结论或步骤，能短则短，不铺陈、不客套。",
    "normal": "回复适中：说清要点即可，不必过度展开。",
    "detailed": "回复可以详尽展开，给出完整背景、细节与替代方案。",
}

os.makedirs(SESSIONS_DIR, exist_ok=True)
for ai in AI_CONF.values():
    os.makedirs(ai["workspace"], exist_ok=True)
    os.makedirs(os.path.join(ai["workspace"], "memory"), exist_ok=True)

# ---------------------------------------------------------------------------
# 1. 系统提示（载入各自身份文件，组合成“真身”persona）
# ---------------------------------------------------------------------------
def build_system_prompt(ai_key: str) -> str:
    ai = AI_CONF[ai_key]
    parts = []
    for pf in ai.get("persona_files", []):
        if os.path.isfile(pf):
            try:
                with open(pf, "r", encoding="utf-8") as fh:
                    parts.append(f"# 身份文件：{os.path.basename(pf)}\n{fh.read().strip()}")
            except Exception as e:
                parts.append(f"[读取 {pf} 失败: {e}]")
    persona = "\n\n".join(parts)

    roster = "、".join(
        f"{a['name']} {a.get('emoji','')}（前缀 {a.get('prefix')}）"
        for k, a in AI_CONF.items() if k != ai_key)
    common = f"""
你正在一个本地网页程序「多 AI 工作台」中运行，你的名字是「{ai['name']} {ai.get('emoji','')}」。
本程序里其他 AI 成员：{roster}。
你的工作区目录是：{ai['workspace']}（可用工具在此目录及共享记忆区自由读写）。
共享记忆区（所有 AI 共用的文件夹）是：{SHARED_MEMORY}。

【你要做的】
- 像你自己，用你本来的性格和口吻说话。
- 你拥有工具能力，可以真正“干活”：read_file / read_lines（读指定行）、write_file、edit_file（**只替换其中一段**，不整篇重写）、move_rename_file（移动/改名）、list_dir、search_workspace（目录内全文搜索）、run_command、search_memory / append_memory。
- 改文件的小改动优先用 edit_file（省 token，也不会覆盖掉别人写的内容）；找东西先 search_workspace 定位、再 read_lines 看细节；查外部资料用 web_search，读某个具体网页/接口用 fetch_url。
- 当用户或其他 AI 让你做事时，优先用工具去完成，而不是只给建议。
- **如果只是回答问题、不需要读写文件时，直接回答，不要为了"显得在工作"而调用工具**——每多一次工具调用就多一轮等待，用户会觉得卡。
- **动手前先说一句你的思路/打算怎么做**（一句话即可，会显示在"思考"区域），让用户能看见你的判断过程，而不是干等。
- 涉及跨 AI 的协作信息、约定、结论，写到共享记忆区（append_memory / search_memory）。
- 【回复长度】{REPLY_LEN_HINT.get(SETTINGS.get("replyLength", "concise"), REPLY_LEN_HINT["concise"])}

【共享记忆区铁律】
- 共享记忆区在本程序内：{SHARED_MEMORY}
- 你只能写“你自己前缀”的文件：{ai.get('prefix')}。不要创建或覆盖别人前缀的文件。
- append_memory 会自动按你的身份加前缀；若用 write_file 直接写共享记忆区，文件名必须以你的前缀开头，否则会被拒绝。
- 读别人的文件没问题，但别改、别覆盖。

【安全边界】
- run_command 只在用户本机执行，仅用于开发/文件类合理操作，不要做破坏性操作（如格式化、删系统盘）。
- 私人的东西就是私人的，不要对外发送任何内容。

现在开始。
"""
    return persona + "\n\n" + common


SYSTEM_PROMPTS = {k: build_system_prompt(k) for k in AI_CONF}

def rebuild_prompts():
    """设置/成员变更后重建系统提示（例如用户改了回复长度偏好、加了新 AI）"""
    global SYSTEM_PROMPTS
    SYSTEM_PROMPTS = {k: build_system_prompt(k) for k in AI_CONF}

# 互聊/群聊冷启动时，对"同场其他 AI"的介绍（按当前花名册动态生成）
def peer_intro(ai_key, agents=None):
    me = AI_CONF[ai_key]
    others = [k for k in (agents or AGENT_ORDER) if k != ai_key and k in AI_CONF]
    names = "、".join(f"「{AI_CONF[k]['name']}({AI_CONF[k].get('emoji','')})」" for k in others)
    return (f"同场合作的其他 AI：{names}。你们共用共享记忆文件夹：{SHARED_MEMORY}。"
            f"写共享记忆时你的文件须带 {me.get('prefix')} 前缀，别人的带他们各自前缀，互不覆盖。")

# ---------------------------------------------------------------------------
# 2. 工具定义（OpenAI / DeepSeek 兼容的函数调用格式）
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取一个文本文件的全部内容。可读取工作区或共享记忆区的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径，例如 <某个工作区目录>/note.md"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容写入一个文件（若不存在则创建，若存在则覆盖）。用于真正“干活”、产出文件。"
                           "支持 mode：write/overwrite=覆盖写入，append=在文件末尾追加（适合被输出上限截断后分块续写）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径"},
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                    "mode": {"type": "string", "enum": ["write", "overwrite", "append"],
                             "description": "写入模式：write/overwrite=覆盖（默认），append=追加到末尾"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录下的文件和子目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录绝对路径"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在本机执行一条命令行命令（用于开发/文件处理等合理操作）。仅返回 stdout/stderr，超时 30 秒。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令，例如 python -c \"print(1+1)\""}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "在共享记忆区（你和另一个 AI 共用的文件夹）里按关键词检索文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "append_memory",
            "description": "向共享记忆区追加一条长期记录（按当天日期存为一个 md 文件）。用于沉淀结论、约定、协作信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要记录的内容"}
                },
                "required": ["text"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "在已有文件里把一段「原文本」替换成「新文本」（外科手术式修改，不整文件覆盖）。"
                           "适合多人协作改同一文件、或只改其中几行。old_text 必须与原文件某处完全一致"
                           "（含缩进与换行）。replace_all=true 时替换全部匹配，否则只替换第一处。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径"},
                    "old_text": {"type": "string", "description": "要被替换掉的原文片段（精确匹配）"},
                    "new_text": {"type": "string", "description": "替换后的新内容"},
                    "replace_all": {"type": "boolean", "description": "是否替换全部匹配（默认否，只改第一处）"}
                },
                "required": ["path", "old_text", "new_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "move_rename_file",
            "description": "移动或重命名一个文件（原子替换）。用于整理工作区、给产出文件改名。",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "源文件绝对路径"},
                    "dst": {"type": "string", "description": "目标绝对路径"}
                },
                "required": ["src", "dst"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_lines",
            "description": "只读文件的指定行区间（行号从 1 开始）。补足 read_file 的截断限制，"
                           "适合大文件/代码精准定位某几行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径"},
                    "start_line": {"type": "integer", "description": "起始行（含，从 1 开始）"},
                    "end_line": {"type": "integer", "description": "结束行（含，不填则读到文件末尾）"}
                },
                "required": ["path", "start_line"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_workspace",
            "description": "在指定目录下递归搜索文件内容（按关键词），可按文件扩展名过滤。"
                           "返回命中行与行号。用于跨多个文件定位信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要搜索的目录绝对路径"},
                    "query": {"type": "string", "description": "搜索关键词（小写不敏感）"},
                    "ext": {"type": "string", "description": "可选，只搜这些扩展名，逗号分隔，如 md,py,txt"},
                    "max_hits": {"type": "integer", "description": "最多返回条数（默认 50）"}
                },
                "required": ["path", "query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "抓取一个网页/接口地址的文本内容（自动去 HTML 标签、转成可读文本）。"
                           "用于直接读取某个已知网址的页面。本机代理可能受限，失败会回显原因。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "以 http:// 或 https:// 开头的网址"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索关键词，返回结果标题/链接/摘要。用于查资料、找官方文档。"
                           "本机代理可能受限，失败会回显原因并建议你改用 fetch_url 抓已知页面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词"}
                },
                "required": ["query"]
            }
        }
    }
]

# ---------------------------------------------------------------------------
# 3. 工具执行
# ---------------------------------------------------------------------------
def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ---- 文件占用登记：避免多个 AI 同时改同一文件互相覆盖 ----
EDITORS_PATH = os.path.join(BASE_DIR, ".editors.json")
SMALL_FILE_LIMIT = 2 * 1024 * 1024 * 1024   # 2GB：低于此值走"副本→原子替换"
CLAIM_TTL = 180                              # 登记有效期（秒）
MAX_READ_CHARS = 64 * 1024                   # read_file 单次读取上限，防大文件塞爆上下文

def _load_editors():
    if not os.path.isfile(EDITORS_PATH):
        return {}
    try:
        with open(EDITORS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_editors(d):
    try:
        with open(EDITORS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _claim_file(path, who):
    """登记"我在改这个文件"。若别人近期也在改 → 返回对方名字，否则 None"""
    with _lock:
        d = _load_editors()
        now = time.time()
        rec = d.get(path)
        if rec and rec.get("who") != who and (now - rec.get("t", 0)) < CLAIM_TTL:
            return rec.get("who")
        d[path] = {"who": who, "t": now}
        _save_editors(d)
        return None

def _release_file(path, who):
    with _lock:
        d = _load_editors()
        if d.get(path, {}).get("who") == who:
            d.pop(path, None)
            _save_editors(d)

def exec_tool(name, args, ai_key="agent_a"):
    """执行一个工具，返回 (ok, result_text)。ai_key 用于共享记忆区的前缀铁律。"""
    ag = AI_CONF.get(ai_key, {})
    # 署名缩写前缀：取 agent 自己的 prefix 字段（如 BC_ / JD_，新成员自动生成）
    prefix = ag.get("prefix", ai_key[:2].upper() + "_")
    who = ag.get("name", ai_key)
    try:
        if name == "read_file":
            p = args["path"]
            # ① 防特殊文件中断：先排除目录，避免把目录当文件读把整个群聊卡死
            if os.path.isdir(p):
                return False, (f"⛔ 这是一个目录，不是文件：{p}\n"
                               f"（用 list_dir 查看目录内容，或用 search_memory 在记忆区搜索）")
            if not os.path.isfile(p):
                return False, f"⛔ 文件不存在或无法访问：{p}"
            try:
                size = os.path.getsize(p)
            except OSError as e:
                return False, f"⛔ 无法读取文件信息：{e}"
            # 超大文件直接拒读（不再整文件读进内存），避免几百 MB 把内存/上下文撑爆、
            # 进而让 SSE 流中断（这就是「碰到特殊文件就断」的主因）
            MAX_SAFE = 256 * 1024 * 1024   # 256MB
            if size > MAX_SAFE:
                return False, (f"⛔ 文件过大（{size/1024/1024:.1f} MB），超过 256MB 安全上限，已跳过读取。\n"
                               f"如果是日志/数据/构建产物，请用 run_command 配合 head/tail/grep 指定更小范围，"
                               f"或告诉我你想看哪一段。")
            # 只读前 N 字符（不整文件读），设备/管道/二进制锁文件也不会把内存吃满
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(MAX_READ_CHARS)
            except Exception as e:
                return False, (f"⛔ 读取失败（可能是设备文件/无权限/二进制锁）：{e}\n"
                               f"（二进制文件请用 run_command 配合 strings/xxd 处理）")
            if size > MAX_READ_CHARS:
                text += (f"\n\n…[内容过长，已截断：原文件 {size} 字节，只读了前 {MAX_READ_CHARS} 字符；"
                         f"如需更多片段，请用 run_command 配合 sed -n 'a,bp' / grep -n 指定区间]")
            return True, text
        elif name == "write_file":
            p = args.get("path")
            if not p:
                return False, "⛔ write_file 缺少必需参数 path（文件路径）"
            content = args.get("content")
            if content is None:
                return False, "⛔ write_file 缺少必需参数 content（要写入的文本）"
            mode = str(args.get("mode", "write")).lower()
            if mode not in ("write", "overwrite", "append"):
                return False, f"⛔ write_file 的 mode 只能是 write/overwrite/append，收到：{mode}"
            norm_p = os.path.normpath(p)
            # 防「特殊文件名」：Windows 非法字符 / 保留设备名 / 结尾点空格。
            # 提前拦下并明确告诉模型怎么改名（模型能看懂、能自我纠正），
            # 不再让 OSError 靠运气被上层兜住。
            try:
                _drive, _rest = os.path.splitdrive(norm_p)
                for _seg in [x for x in _rest.split(os.sep) if x]:
                    if any((c in '<>:"|?*') or (ord(c) < 32) for c in _seg):
                        return False, (f"⛔ 路径段「{_seg}」含 Windows 不允许的字符（<>:\"|?* 或控制符）。"
                                       f"请改用中文、中划线或下划线命名，例如「住宿-美食-门票.md」。")
                    if _seg != _seg.rstrip() or _seg.endswith("."):
                        return False, f"⛔ 文件名「{_seg}」不能以空格或点结尾，请改名后重试。"
                    if _seg.split(".")[0].upper() in (
                            "CON", "PRN", "AUX", "NUL",
                            "COM1", "COM2", "COM3", "COM4", "COM5", "COM6",
                            "COM7", "COM8", "COM9",
                            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6",
                            "LPT7", "LPT8", "LPT9"):
                        return False, f"⛔ 「{_seg}」是 Windows 保留设备名，不能当文件名，请换个名字。"
            except Exception as _perr:
                return False, f"⛔ 路径不合法：{_perr}"
            # —— 可选扩展：接管「改本程序代码」的请求（默认关闭）——
            # 仅当存在 extensions/ 目录且里面有 router.py 时才生效。
            if _EXT_ROUTER is not None and _EXT_ROUTER.is_program_code(norm_p):
                return _EXT_ROUTER.route(norm_p, content, who, prefix)
            # 防撞锁：外部程序改代码时会放 .external_editing，此时禁止非可信 AI 改同目录代码
            lock = os.path.join(BASE_DIR, ".external_editing")
            if (norm_p.startswith(BASE_DIR) and not ag.get("trusted")
                    and os.path.isfile(lock)):
                try:
                    owner = open(lock, encoding="utf-8").read().strip()
                except Exception:
                    owner = "（外部程序正在改代码）"
                return False, (f"⛔ 本程序代码正被外部编辑：{owner}。"
                               f"为避免互相覆盖，请稍后再改，或先在群聊里说明你要改什么、改哪个文件。")
            # 共享记忆区铁律：只能写自己前缀的文件，不能覆盖对方
            if norm_p == SHARED_MEMORY or norm_p.startswith(SHARED_MEMORY + os.sep):
                bn = os.path.basename(norm_p)
                if not bn.startswith(prefix):
                    return False, (f"⛔ 共享记忆区铁律：你（{who}）只能写自己前缀的文件"
                                   f"（{prefix}*），不能写/覆盖 {bn}。请用 {prefix} 开头命名。")
            # 确认本文件只有自己在用（别的 AI 近期登记过就先让路）
            owner = _claim_file(norm_p, who)
            if owner:
                return False, (f"⛔ 「{owner}」正在改这个文件：{p}\n"
                               f"为避免互相覆盖，请等它改完（约 {CLAIM_TTL} 秒无动作会自动释放），"
                               f"或在群聊里跟它协商好谁改。")
            try:
                os.makedirs(os.path.dirname(norm_p) or ".", exist_ok=True)
                if mode == "append":
                    # 追加模式：在文件末尾续写，适合被 max_tokens 截断后分块续写
                    with open(norm_p, "ab") as fh:
                        fh.write(content.encode("utf-8"))
                    how = "追加(append)写入"
                else:
                    size = os.path.getsize(norm_p) if os.path.isfile(norm_p) else 0
                    if size < SMALL_FILE_LIMIT:
                        # 小文件：先写副本，确认无他人占用后原子替换（不会写到一半被别人读到）
                        tmp = f"{norm_p}.tmp_{ai_key}_{int(time.time()*1000)}"
                        with open(tmp, "wb") as fh:
                            fh.write(content.encode("utf-8"))
                        os.replace(tmp, norm_p)      # 原子替换
                        how = "副本写入→确认占用→原子替换"
                    else:
                        # 大文件（≥2G）：复制成本太高，确认无人占用后直接改本体
                        os.makedirs(os.path.dirname(norm_p) or ".", exist_ok=True)
                        with open(norm_p, "wb") as fh:
                            fh.write(content.encode("utf-8"))
                        how = "大文件(≥2G)→确认占用→直写本体"
                # D：回读校验 + 真实字节数，确认真实落盘，杜绝「静默不落盘」
                real = os.path.getsize(norm_p)
                wrote = len(content)
                exp_bytes = len(content.encode("utf-8"))
                verify_note = ""
                if mode != "append" and real != exp_bytes:
                    verify_note = (f" ⚠️回读字节数({real})与预期({exp_bytes})不一致，可能写入异常，"
                                   f"请重试或改用 mode=append 分块写")
                return True, (f"已写入：{p}（本次写入 {wrote} 字符 / {exp_bytes} 字节；"
                              f"文件现共 {real} 字节；{how}；已确认无他人占用）{verify_note}")
            finally:
                _release_file(norm_p, who)
        elif name == "list_dir":
            d = args["path"]
            if not os.path.isdir(d):
                return False, f"不是目录或不存在：{d}"
            items = []
            for entry in sorted(os.listdir(d)):
                full = os.path.join(d, entry)
                items.append(entry + ("/" if os.path.isdir(full) else ""))
            return True, "\n".join(items) if items else "（空目录）"
        elif name == "run_command":
            cmd = args["command"]
            # 防大输出爆内存 + 防挂死：命令若「不输出也不退出」（等 stdin / 读锁文件 /
            # 起了交互程序），旧的边读边收循环会永远卡住，把整条 SSE 流拖死 ——
            # 用户看到的就是「碰到特殊文件进程就死了」。现在：stdin 接黑洞 +
            # 读输出放守护线程 + 全局 30 秒硬超时，超时按进程树整棵击杀，绝不悬挂。
            MAX_OUT = 8000
            CMD_TIMEOUT = 30
            try:
                proc = subprocess.Popen(cmd, shell=True,
                                        stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace",
                                        bufsize=1, cwd=BASE_DIR)
            except Exception as e:
                return False, f"⛔ 命令启动失败：{e}"

            def _kill_tree(_p):
                # 杀整棵树：shell=True 下 proc 只是 cmd.exe，孙进程不杀会变孤儿继续占文件
                try:
                    subprocess.run(["taskkill", "/PID", str(_p.pid), "/T", "/F"],
                                   capture_output=True, timeout=8)
                except Exception:
                    try:
                        _p.kill()
                    except Exception:
                        pass

            holder = {"lines": [], "total": 0, "truncated": False}
            _rd_done = threading.Event()

            def _reader():
                try:
                    for line in proc.stdout:
                        holder["total"] += len(line)
                        if holder["total"] > MAX_OUT:
                            holder["truncated"] = True
                            break
                        holder["lines"].append(line)
                except Exception:
                    pass
                finally:
                    _rd_done.set()

            threading.Thread(target=_reader, daemon=True).start()
            if not _rd_done.wait(CMD_TIMEOUT):
                # 30 秒没读完整（命令挂死）→ 杀树，把已收集到的输出还给模型
                _kill_tree(proc)
                _rd_done.wait(5)
                out = "".join(holder["lines"])
                note = (f"\n\n⛔ 命令超过 {CMD_TIMEOUT} 秒未结束，已强制终止（杀掉整个进程树）；"
                        f"以上是终止前已产出的输出。")
                return True, (out if out.strip() else "（超时终止，无输出）") + note
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
            out = "".join(holder["lines"])
            if holder["truncated"]:
                out += (f"\n\n…[输出过长，已超过 {MAX_OUT} 字符已截断；"
                        f"如需完整输出请缩小范围（如 head/tail/grep 指定文件或目录）]")
            # 注：不在工具层同步（log_and_save 统一同步，避免重复）
            return True, out if out.strip() else "（无输出）"
        elif name == "search_memory":
            q = args["query"].lower()
            hits = []
            if os.path.isdir(SHARED_MEMORY):
                for root, _, files in os.walk(SHARED_MEMORY):
                    for fn in files:
                        if fn.endswith((".md", ".txt", ".json")):
                            fp = os.path.join(root, fn)
                            try:
                                sz = os.path.getsize(fp)
                            except OSError:
                                continue
                            # 单文件 >4MB 跳过搜索（防卡死），只读前 2MB
                            if sz > 4 * 1024 * 1024:
                                continue
                            try:
                                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                                    data = fh.read(2 * 1024 * 1024)
                            except Exception:
                                continue
                            for i, line in enumerate(data.splitlines(), 1):
                                if q in line.lower():
                                    hits.append(f"{fp} (行{i}): {line.strip()[:120]}")
                                    if len(hits) >= 30:
                                        break
                            if len(hits) >= 30:
                                break
                    if len(hits) >= 30:
                        break
            if not hits:
                return True, "共享记忆区未找到匹配内容。"
            return True, "\n".join(hits[:30])
        elif name == "append_memory":
            text = args["text"]
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            # 按身份自动加前缀：如 BC_<日期>.md / JD_<日期>.md（只写自己的文件）
            fp = os.path.join(SHARED_MEMORY, f"{prefix}{today}.md")
            os.makedirs(SHARED_MEMORY, exist_ok=True)
            with open(fp, "a", encoding="utf-8") as fh:
                fh.write(f"\n- [{_now()}] [{who}] {text}\n")
            return True, f"已追加到共享记忆：{fp}（署名前缀 {prefix}）"
        elif name == "read_lines":
            p = args.get("path")
            if not p:
                return False, "⛔ read_lines 缺少 path"
            if os.path.isdir(p):
                return False, f"⛔ 这是目录不是文件：{p}（用 list_dir 查看）"
            if not os.path.isfile(p):
                return False, f"⛔ 文件不存在或无法访问：{p}"
            try:
                size = os.path.getsize(p)
            except OSError as e:
                return False, f"⛔ 无法读取文件信息：{e}"
            if size > 256 * 1024 * 1024:
                return False, (f"⛔ 文件过大（{size/1024/1024:.1f} MB），超过 256MB 安全上限；"
                               f"请用 run_command 配合 sed -n / grep 指定区间。")
            try:
                start = int(args.get("start_line", 1))
                end = args.get("end_line")
                end = int(end) if end not in (None, "") else None
            except Exception:
                return False, "⛔ start_line / end_line 必须是正整数"
            if start < 1:
                return False, "⛔ start_line 必须 ≥ 1"
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except Exception as e:
                return False, f"⛔ 读取失败（可能二进制/无权限）：{e}"
            total = len(lines)
            if end is None or end > total:
                end = total
            if start > total:
                return True, f"（文件共 {total} 行，start_line {start} 超出范围，无内容）"
            seg = lines[start - 1:end]
            return True, ("\n".join(f"{i}|{ln}" for i, ln in enumerate(seg, start))
                          + f"\n\n[行 {start}-{end} / 共 {total} 行]")

        elif name == "edit_file":
            p = args.get("path")
            old = args.get("old_text")
            new = args.get("new_text")
            if not p:
                return False, "⛔ edit_file 缺少 path"
            if old is None:
                return False, "⛔ edit_file 缺少 old_text（要被替换的原文片段）"
            if new is None:
                return False, "⛔ edit_file 缺少 new_text（替换后的新内容）"
            if os.path.isdir(p):
                return False, f"⛔ 这是目录不是文件：{p}"
            if not os.path.isfile(p):
                return False, f"⛔ 文件不存在：{p}（新建请用 write_file）"
            norm_p = os.path.normpath(p)
            try:
                _drive, _rest = os.path.splitdrive(norm_p)
                for _seg in [x for x in _rest.split(os.sep) if x]:
                    if any((c in '<>:"|?*') or (ord(c) < 32) for c in _seg):
                        return False, f"⛔ 路径段「{_seg}」含 Windows 非法字符（<>:\"|?* 或控制符），请改名"
                    if _seg != _seg.rstrip() or _seg.endswith("."):
                        return False, f"⛔ 文件名「{_seg}」不能以空格或点结尾，请改名"
                    if _seg.split(".")[0].upper() in (
                            "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3",
                            "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6",
                            "LPT7", "LPT8", "LPT9"):
                        return False, f"⛔ 「{_seg}」是 Windows 保留设备名，不能当文件名"
            except Exception as _perr:
                return False, f"⛔ 路径不合法：{_perr}"
            try:
                size = os.path.getsize(norm_p)
                if size > 256 * 1024 * 1024:
                    return False, f"⛔ 文件过大（{size/1024/1024:.1f} MB），超过 256MB 安全上限"
                with open(norm_p, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except Exception as e:
                return False, f"⛔ 读取失败（可能二进制/无权限）：{e}"
            if old not in content:
                return False, ("⛔ 在文件中找不到 old_text 的精确匹配。请确认片段完全一致"
                               "（含缩进/换行）。文件前 400 字符预览：\n" + content[:400])
            replace_all = bool(args.get("replace_all", False))
            cnt = content.count(old)
            if replace_all:
                new_content = content.replace(old, new)
            else:
                idx = content.index(old)
                new_content = content[:idx] + new + content[idx + len(old):]
            # 程序代码的改动交给可选扩展接管（与原 write_file 行为一致）
            if _EXT_ROUTER is not None and _EXT_ROUTER.is_program_code(norm_p):
                return _EXT_ROUTER.route(norm_p, new_content, who, prefix)
            lock = os.path.join(BASE_DIR, ".external_editing")
            if (norm_p.startswith(BASE_DIR) and not ag.get("trusted")
                    and os.path.isfile(lock)):
                try:
                    owner = open(lock, encoding="utf-8").read().strip()
                except Exception:
                    owner = "（外部编辑器正在改代码）"
                return False, (f"⛔ 程序代码正被外部编辑器改动：{owner}。"
                               f"请稍后再改，或先在群聊里说明要改什么。")
            if norm_p == SHARED_MEMORY or norm_p.startswith(SHARED_MEMORY + os.sep):
                bn = os.path.basename(norm_p)
                if not bn.startswith(prefix):
                    return False, (f"⛔ 共享记忆区铁律：你（{who}）只能写自己前缀文件"
                                   f"（{prefix}*），不能改 {bn}")
            owner = _claim_file(norm_p, who)
            if owner:
                return False, (f"⛔ 「{owner}」正在改这个文件：{p}\n"
                               f"为避免互相覆盖请稍后，或协商好谁改")
            try:
                if len(new_content) < SMALL_FILE_LIMIT:
                    tmp = f"{norm_p}.tmp_{ai_key}_{int(time.time()*1000)}"
                    with open(tmp, "wb") as fh:
                        fh.write(new_content.encode("utf-8"))
                    os.replace(tmp, norm_p)
                else:
                    with open(norm_p, "wb") as fh:
                        fh.write(new_content.encode("utf-8"))
                real = os.path.getsize(norm_p)
                return True, (f"已替换：{p}（{'全部 '+str(cnt)+' 处' if replace_all else '第 1 处，共 '+str(cnt)+' 处'}；"
                              f"新文件 {real} 字节；已确认无他人占用）")
            finally:
                _release_file(norm_p, who)

        elif name == "move_rename_file":
            srcp = args.get("src")
            dstp = args.get("dst")
            if not srcp or not dstp:
                return False, "⛔ move_rename_file 需要 src 和 dst"
            if not os.path.isfile(srcp):
                return False, f"⛔ 源文件不存在：{srcp}"
            nsrc, ndst = os.path.normpath(srcp), os.path.normpath(dstp)
            try:
                _drive, _rest = os.path.splitdrive(ndst)
                for _seg in [x for x in _rest.split(os.sep) if x]:
                    if any((c in '<>:"|?*') or (ord(c) < 32) for c in _seg):
                        return False, f"⛔ 目标路径段「{_seg}」含 Windows 非法字符，请改名"
                    if _seg != _seg.rstrip() or _seg.endswith("."):
                        return False, f"⛔ 目标文件名「{_seg}」不能以空格或点结尾"
                    if _seg.split(".")[0].upper() in (
                            "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3",
                            "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6",
                            "LPT7", "LPT8", "LPT9"):
                        return False, f"⛔ 「{_seg}」是保留设备名，不能当文件名"
            except Exception as _perr:
                return False, f"⛔ 目标路径不合法：{_perr}"
            if (ndst.startswith(BASE_DIR) and not ag.get("trusted")
                    and os.path.isfile(os.path.join(BASE_DIR, ".external_editing"))):
                return False, "⛔ 程序代码正被外部编辑器改动，暂不能移动该目录下文件，请稍后"
            if ndst == SHARED_MEMORY or ndst.startswith(SHARED_MEMORY + os.sep):
                bn = os.path.basename(ndst)
                if not bn.startswith(prefix):
                    return False, (f"⛔ 共享记忆区铁律：你（{who}）只能写自己前缀文件"
                                   f"（{prefix}*），不能把文件移入 {bn}")
            o1 = _claim_file(nsrc, who)
            o2 = _claim_file(ndst, who)
            if o1 or o2:
                if o1: _release_file(nsrc, who)
                if o2: _release_file(ndst, who)
                return False, f"⛔ 别人正在操作该文件（{o1 or o2}），请稍后"
            try:
                os.makedirs(os.path.dirname(ndst) or ".", exist_ok=True)
                os.replace(nsrc, ndst)
                return True, f"已移动/重命名：{srcp} → {dstp}"
            except OSError as e:
                return False, (f"⛔ 移动失败（跨盘/权限/占用？）：{e}\n"
                               f"若跨磁盘移动，可改用 run_command 的 move / xcopy")
            finally:
                _release_file(nsrc, who)
                _release_file(ndst, who)

        elif name == "search_workspace":
            d = args.get("path")
            q = (args.get("query") or "").lower()
            if not d or not os.path.isdir(d):
                return False, f"⛔ path 不是目录或不存在：{d}"
            if not q:
                return False, "⛔ search_workspace 缺少 query"
            exts = args.get("ext")
            ext_set = None
            if exts:
                ext_set = set("." + e.lstrip(".") for e in str(exts).split(",") if e.strip())
            try:
                max_hits = max(1, min(200, int(args.get("max_hits", 50))))
            except Exception:
                max_hits = 50
            hits = []
            try:
                for root, _, files in os.walk(d):
                    for fn in files:
                        if ext_set and not any(fn.endswith(e) for e in ext_set):
                            continue
                        fp = os.path.join(root, fn)
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            continue
                        if sz > 8 * 1024 * 1024:
                            continue
                        try:
                            data = open(fp, "r", encoding="utf-8", errors="replace").read(8 * 1024 * 1024)
                        except Exception:
                            continue
                        low = data.lower()
                        pos = 0
                        while True:
                            i = low.find(q, pos)
                            if i < 0:
                                break
                            line_no = data.count("\n", 0, i) + 1
                            s = data.rfind("\n", 0, i) + 1
                            e = data.find("\n", i)
                            e = len(data) if e < 0 else e
                            hits.append(f"{fp} (行{line_no}): {data[s:e].strip()[:140]}")
                            if len(hits) >= max_hits:
                                break
                            pos = i + len(q)
                        if len(hits) >= max_hits:
                            break
                    if len(hits) >= max_hits:
                        break
            except Exception as e:
                return False, f"⛔ 搜索出错：{e}"
            if not hits:
                return True, f"在 {d} 下未找到匹配「{q}」的内容。"
            return True, "\n".join(hits[:max_hits]) + f"\n\n[共 {len(hits)} 条命中，上限 {max_hits}]"

        elif name == "fetch_url":
            url = args.get("url")
            if not url:
                return False, "⛔ fetch_url 缺少 url"
            if not (url.startswith("http://") or url.startswith("https://")):
                return False, "⛔ url 必须以 http:// 或 https:// 开头"
            try:
                # 绕开本机可能失效的系统代理（ProxyHandler({})），否则易被劫持/报 502
                _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                _reqq = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 (compatible; TwinAI/1.0)"})
                with _opener.open(_reqq, timeout=20) as _resp:
                    _code = getattr(_resp, "status", "?")
                    _charset = _resp.headers.get_content_charset() or "utf-8"
                    raw = _resp.read(4 * 1024 * 1024).decode(_charset, errors="replace")
            except Exception as e:
                return False, (f"⛔ 抓取失败（本机网络/代理可能受限）：{e}\n"
                               f"提示：确认该网址能在你本机浏览器打开；或改用 fetch_url 抓镜像。")
            import re
            body = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
            body = re.sub(r"(?is)<!--.*?-->", " ", body)
            body = re.sub(r"(?s)<[^>]+>", " ", body)
            for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
                body = body.replace(a, b)
            body = "\n".join(ln.strip() for ln in body.splitlines() if ln.strip())
            MAX = 20000
            if len(body) > MAX:
                body = body[:MAX] + f"\n\n…[正文过长，已截断到 {MAX} 字符]"
            return True, f"[已抓取 {url}｜HTTP {_code}｜{len(body)} 字符]\n\n" + body

        elif name == "web_search":
            q = args.get("query")
            if not q:
                return False, "⛔ web_search 缺少 query"
            import re
            html = ""
            last_err = ""
            # 多个搜索源依次尝试：国内网络下 cn.bing.com 最稳（DDG 常被墙，已不作主源）
            for _base in ("https://cn.bing.com/search?q=", "https://www.bing.com/search?q="):
                try:
                    from urllib.parse import quote as _quote
                    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    _reqq = urllib.request.Request(
                        _base + _quote(q),
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                                 "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
                    with _opener.open(_reqq, timeout=20) as _resp:
                        html = _resp.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
                    if 'class="b_algo"' in html:
                        break
                except Exception as e:
                    last_err = str(e)
            items = []
            for b in re.findall(r'<li class="b_algo".*?</li>', html, re.S)[:10]:
                m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
                if not m:
                    continue
                href = m.group(1)
                title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                p = re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
                snip = re.sub(r"<[^>]+>", "", p.group(1)) if p else ""
                snip = re.sub(r"\s+", " ", snip).replace("&ensp;", " ").replace("&#0183;", "·").strip()
                items.append(f"{len(items)+1}. {title}\n   {href}\n   {snip[:200]}")
            if not items:
                return False, (f"⛔ 搜索失败（网络/代理受限，多搜索源均不可达）：{last_err or '无结果'}\n"
                               f"提示：可改用 fetch_url 直接抓已知网址（如官网/文档站）。")
            return True, "\n".join(items)

        elif name.startswith("mcp__"):
            return mcp_exec(name, args)
        else:
            return False, f"未知工具：{name}"
    except Exception as e:
        return False, f"工具执行出错：{repr(e)}"

# ---------------------------------------------------------------------------
# 3.5 外部能力：MCP（Model Context Protocol）客户端 + DSH 插件发现
#     目的：直接用别人已经写好的 MCP 服务，不自己重复造轮子。
#     MCP 是行业标准，社区有海量现成 server（文件/搜索/数据库/GitHub/浏览器…），
#     DSH 生态里凡是 MCP 形态的插件，也能原样接进来。
# ---------------------------------------------------------------------------
import json as _mjson
import subprocess as _msp
import threading as _mth
import queue as _mq
import re as _mre
import hashlib as _mhash
import io as _mio
import sys as _msys

MCP_TIMEOUT = 45          # 单次工具调用超时（秒）
MCP_HANDSHAKE_TIMEOUT = 30
MCP_MAX_TOOLS = 60        # 单个 server 最多注册多少工具
MCP_MAX_TEXT = 8000       # 单条工具返回最大字符

MCP_TOOLS = []            # 已注册的 MCP 工具（OpenAI function 格式）
_MCP_PROCS = {}           # server 名 -> {"proc": Popen, "cfg": dict, "tools": [...]}
_MCP_ERR = {}             # server 名 -> 最近一次错误文本
_MCP_DIRTY = True         # 配置变过 → 下次取工具表时重连
_MCP_ID = [1]


def _mcp_next_id():
    _MCP_ID[0] += 1
    return _MCP_ID[0]


def _mcp_cfg():
    """配置里的 mcpServers：{ 名: {command,args,env,cwd,url,headers,disabled} }"""
    c = SETTINGS.get("mcpServers")
    if not isinstance(c, dict):
        c = {}
    return {str(k): v for k, v in c.items() if isinstance(v, dict)}


def _mcp_public_cfg(c):
    """给前端看的配置（隐藏可能的敏感 env）"""
    env = c.get("env") or {}
    return {
        "command": c.get("command") or "",
        "args": c.get("args") or [],
        "cwd": c.get("cwd") or "",
        "url": c.get("url") or "",
        "envKeys": sorted([str(k) for k in env]) if isinstance(env, dict) else [],
    }


def _mcp_spawn(cfg):
    cmd = str(cfg.get("command") or "")
    if not cmd:
        return None, "缺少 command"
    # 写 python 的人多半是想要「跑工作台的这个解释器」，直接用绝对路径最稳
    if cmd.lower() in ("python", "python3", "py"):
        cmd = _msys.executable
    args = [cmd] + [str(a) for a in (cfg.get("args") or [])]
    env = os.environ.copy()
    e = cfg.get("env") or {}
    if isinstance(e, dict):
        env.update({str(k): str(v) for k, v in e.items()})
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW，别弹黑框
    try:
        p = _msp.Popen(args, stdin=_msp.PIPE, stdout=_msp.PIPE,
                       stderr=_msp.DEVNULL, env=env,
                       cwd=(cfg.get("cwd") or None),
                       text=True, encoding="utf-8", errors="replace",
                       bufsize=1, **kw)
    except Exception as ex:
        return None, "启动失败：%r" % (ex,)
    return p, ""


def _mcp_send(p, obj):
    p.stdin.write(_mjson.dumps(obj, ensure_ascii=False) + "\n")
    p.stdin.flush()


def _mcp_recv(p, timeout=MCP_TIMEOUT):
    """读一行 JSON-RPC 响应（带超时；跳过噪声行）"""
    box = _mq.Queue()

    def _rd():
        try:
            box.put(("ok", p.stdout.readline()))
        except Exception as ex:
            box.put(("err", repr(ex)))

    t = _mth.Thread(target=_rd, daemon=True)
    t.start()
    try:
        kind, line = box.get(timeout=timeout)
    except Exception:
        raise RuntimeError("读取超时（%s 秒无响应）" % timeout)
    if kind == "err":
        raise RuntimeError(line)
    line = (line or "").strip()
    if not line:
        raise RuntimeError("服务已退出或无响应")
    if line.startswith("{"):
        try:
            return _mjson.loads(line)
        except Exception:
            raise RuntimeError("非 JSON 响应：%s" % line[:120])
    raise RuntimeError("非预期响应：%s" % line[:120])


def mcp_connect(name, cfg):
    """启动一个 MCP server 并握手，返回 (tools, err)"""
    p, err = _mcp_spawn(cfg)
    if err:
        return [], err
    try:
        _mcp_send(p, {"jsonrpc": "2.0", "id": _mcp_next_id(), "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "twin-ai", "version": "1.0"}}})
        r = _mcp_recv(p, MCP_HANDSHAKE_TIMEOUT)
        if "error" in r:
            raise RuntimeError(str(r["error"])[:200])
        _mcp_send(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _mcp_send(p, {"jsonrpc": "2.0", "id": _mcp_next_id(),
                      "method": "tools/list", "params": {}})
        r = _mcp_recv(p, MCP_HANDSHAKE_TIMEOUT)
        if "error" in r:
            raise RuntimeError(str(r["error"])[:200])
        tools = (r.get("result") or {}).get("tools") or []
        _MCP_PROCS[name] = {"proc": p, "cfg": cfg, "tools": tools}
        return tools, ""
    except Exception as ex:
        try:
            p.kill()
        except Exception:
            pass
        return [], repr(ex)


def _mcp_kill(name):
    st = _MCP_PROCS.pop(name, None)
    if not st:
        return
    try:
        st["proc"].kill()
    except Exception:
        pass


def _mcp_fname(server, tool):
    """OpenAI 函数名：^[A-Za-z0-9_-]{1,64}$"""
    raw = "mcp__%s__%s" % (server, tool)
    safe = _mre.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if len(safe) <= 64:
        return safe
    h = _mhash.md5(raw.encode("utf-8")).hexdigest()[:8]
    return safe[:64 - 9] + "_" + h


def _mcp_sync_tools():
    """按当前 _MCP_PROCS 重建 MCP_TOOLS"""
    global MCP_TOOLS
    MCP_TOOLS = []
    cfg = _mcp_cfg()
    for name, st in _MCP_PROCS.items():
        if (cfg.get(name) or {}).get("disabled"):
            continue
        for t in (st.get("tools") or [])[:MCP_MAX_TOOLS]:
            tn = t.get("name") or ""
            if not tn:
                continue
            desc = (t.get("description") or tn)
            MCP_TOOLS.append({
                "type": "function",
                "function": {
                    "name": _mcp_fname(name, tn),
                    "description": ("[MCP·%s] " % name) + desc[:380],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                }
            })
    return MCP_TOOLS


def mcp_refresh():
    """按配置全部重连（会先关掉旧进程）"""
    for n in list(_MCP_PROCS):
        _mcp_kill(n)
    for name, c in _mcp_cfg().items():
        if c.get("disabled"):
            continue
        tools, err = mcp_connect(name, c)
        if err:
            _MCP_ERR[name] = err
        else:
            _MCP_ERR.pop(name, None)
    _mcp_sync_tools()
    return MCP_TOOLS


def _mcp_touch():
    """配置变了，标记需要重连"""
    global _MCP_DIRTY
    _MCP_DIRTY = True


def all_tools():
    """内置工具 + MCP 工具（首次使用时懒连接）"""
    global _MCP_DIRTY
    if _MCP_DIRTY:
        _MCP_DIRTY = False
        try:
            mcp_refresh()
        except Exception as ex:
            _MCP_ERR["__refresh__"] = repr(ex)
    return TOOLS + MCP_TOOLS


def mcp_exec(name, args):
    """执行一个 MCP 工具"""
    for sname, st in _MCP_PROCS.items():
        for t in (st.get("tools") or []):
            if _mcp_fname(sname, t.get("name") or "") == name:
                return _mcp_call(sname, st, t.get("name"), args)
    return False, "未找到该 MCP 工具（服务可能未连接）：%s" % name


def _mcp_call(sname, st, tool, args, retry=True):
    p = st.get("proc")
    if p is None or p.poll() is not None:
        if not retry:
            return False, "MCP 服务未运行：%s" % sname
        tools, err = mcp_connect(sname, st.get("cfg") or {})
        if err:
            return False, "重连失败：%s" % err
        st = _MCP_PROCS[sname]
        p = st["proc"]
    try:
        _mcp_send(p, {"jsonrpc": "2.0", "id": _mcp_next_id(), "method": "tools/call",
                      "params": {"name": tool, "arguments": args or {}}})
        r = _mcp_recv(p, MCP_TIMEOUT)
    except Exception as ex:
        return False, "调用出错：%r" % (ex,)
    if "error" in r:
        return False, "MCP 返回错误：%s" % str(r["error"])[:300]
    res = r.get("result") or {}
    parts = []
    for c in (res.get("content") or []):
        if isinstance(c, dict):
            if c.get("type") == "text":
                parts.append(c.get("text") or "")
            else:
                parts.append(_mjson.dumps(c, ensure_ascii=False)[:600])
    txt = ("\n".join(parts) or "（无输出）")[:MCP_MAX_TEXT]
    if res.get("isError"):
        return False, txt
    return True, txt


# ---------------- DSH（DeepSeek Harness）插件发现 ----------------
def _dsh_roots():
    home = os.path.expanduser("~")
    roots = []
    for prof in ("web", "headless", "default", "main"):
        roots.append(os.path.join(home, ".dsh", "profiles", prof, "node_modules"))
    roots.append(os.path.join(home, ".dsh", "plugins"))
    for cand in (os.path.join(BASE_DIR, "engine_plugins"), os.path.join(BASE_DIR, "plugins_ext")):
        roots.append(cand)
    out = []
    for r in roots:
        if os.path.isdir(r) and r not in out:
            out.append(r)
    return out


def _looks_like_mcp(meta):
    try:
        txt = _mjson.dumps(meta, ensure_ascii=False).lower()
    except Exception:
        return False
    return ("modelcontextprotocol" in txt) or ("\"mcp\"" in txt) or ("mcp-server" in txt)


def _dsh_classify(meta):
    """判定：mcp=可直接桥接 / widget=DSH 界面挂件 / lib=普通包"""
    if isinstance(meta.get("mcp"), dict) or isinstance(meta.get("mcpServers"), dict):
        return "mcp", "声明了 MCP 配置 —— 可以直接接入工作台"
    dsh = meta.get("dsh")
    if isinstance(dsh, dict) and dsh.get("bundle"):
        return "widget", ("这是 DSH 的 Cordis 挂件，改的是 DSH 自己的界面，"
                          "属于「缝在 DSH 身上的一块肉」，搬不进工作台。"
                          "想要同款功能请用「工作台插件」重写（更简单）。")
    if meta.get("bin") and _looks_like_mcp(meta):
        return "mcp", "提供可执行入口且疑似 MCP 服务 —— 可以试着接入"
    return "lib", "普通 Node 包，需人工判断用途"


def _dsh_mcp_cmd(d, meta):
    """尽力猜出可运行的 MCP 启动命令"""
    mc = meta.get("mcp")
    if isinstance(mc, dict) and (mc.get("command") or mc.get("args")):
        return {"command": mc.get("command"), "args": mc.get("args") or [],
                "cwd": d, "env": mc.get("env") or {}}
    b = meta.get("bin")
    exe = ""
    if isinstance(b, str):
        exe = os.path.join(d, b)
    elif isinstance(b, dict) and b:
        exe = os.path.join(d, str(list(b.values())[0]))
    if exe:
        return {"command": "node", "args": [exe], "cwd": d, "env": {}}
    main = meta.get("main")
    if main:
        return {"command": "node", "args": [os.path.join(d, str(main))], "cwd": d, "env": {}}
    return None


def dsh_scan():
    """扫描本机 DSH 插件，返回清单（含可接入性判定）"""
    out = []
    seen = set()
    for root in _dsh_roots():
        try:
            names = sorted(os.listdir(root))
        except Exception:
            continue
        for nm in names:
            if nm.startswith(".") or nm.startswith("@") or nm in seen:
                continue
            d = os.path.join(root, nm)
            pj = os.path.join(d, "package.json")
            if not os.path.isfile(pj):
                continue
            try:
                meta = _mjson.load(_mio.open(pj, encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(meta, dict):
                continue
            seen.add(nm)
            kind, reason = _dsh_classify(meta)
            out.append({
                "name": nm,
                "dir": d,
                "version": str(meta.get("version") or ""),
                "description": str(meta.get("description") or "")[:200],
                "kind": kind,
                "reason": reason,
                "mcpCmd": _dsh_mcp_cmd(d, meta) if kind == "mcp" else None,
            })
    return out

# ---------------------------------------------------------------------------
# 4. 与 DeepSeek 对话（含工具循环）
# ---------------------------------------------------------------------------
def _norm_base(u):
    """把「接口地址」收敛成 base。

    用户很容易把完整端点整段粘进来（例如
    https://tokenhub.tencentmaas.com/v1/chat/completions），而调用处会再拼一次
    "/chat/completions"，于是变成 .../chat/completions/chat/completions → 404。
    这里反复剥掉协议路径尾巴，粘什么都不怕。Anthropic 的 /v1/messages 同理。
    """
    s = str(u or "").strip().rstrip("/")
    tails = ("/chat/completions", "/completions", "/responses", "/messages",
             "/embeddings", "/models")
    changed = True
    while changed and s:
        changed = False
        low = s.lower()
        for t in tails:
            if low.endswith(t):
                s = s[: -len(t)].rstrip("/")
                changed = True
                break
    return s


def _http_err(e, url):
    """把 urllib 的 HTTPError 翻译成人能看懂的话 —— 带上服务商返回的 error.message。

    urllib 默认只给 "HTTP Error 404: Not Found"，服务商真正的原因（模型名不对、
    没开通、Key 不匹配…）全在响应体里，不读出来用户就只能瞎猜。
    """
    code = getattr(e, "code", None)
    body = ""
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        pass
    detail = ""
    try:
        j = json.loads(body)
        er = j.get("error") if isinstance(j, dict) else None
        if isinstance(er, dict):
            detail = str(er.get("message") or "")
            if er.get("code"):
                detail = "[%s] %s" % (er.get("code"), detail)
        elif isinstance(er, str):
            detail = er
        elif isinstance(j, dict) and j.get("message"):
            detail = str(j.get("message"))
    except Exception:
        detail = (body or "").strip()[:300]
    hints = {
        400: "请求被拒 —— 最常见是模型名不对：要填平台给的「模型标识」（如 hy4-preview），"
             "不是界面上显示的展示名（如 Hy4 preview）",
        401: "API Key 无效，或这个 Key 不属于该站点（国内站与海外站是独立站点，Key 不通用）",
        402: "该模型未开通或额度不足 —— 去服务商控制台开通该模型 / 领免费额度（与 Key 本身无关）",
        403: "这个 Key 没权限访问该模型，去控制台检查 Key 的可访问范围",
        404: "地址不对 —— 接口地址只填到 /v1 这一层，别带 /chat/completions、/models 这类路径",
        429: "触发限流了，稍等再试",
    }
    hint = hints.get(code, "")
    if not hint and isinstance(code, int) and 500 <= code < 600:
        hint = "服务商侧故障，稍后重试"
    msg = "服务商返回 %s" % code
    if detail:
        msg += "：" + detail
    if hint:
        msg += "\n→ " + hint
    msg += "\n（请求地址：%s）" % url
    return msg


def _net_err(e, url):
    """连不上 / 超时这类网络错误的统一说法。"""
    return ("连不上服务商：%s\n→ 检查接口地址有没有拼错、本机网络或代理设置\n（请求地址：%s）"
            % (getattr(e, "reason", e), url))


def _api_call(messages, tools=None, max_tokens=None, model=None, extra=None, prov=None):
    """非流式调用。prov = 接口提供方（多协议层）；不传则用全局 DeepSeek 配置。"""
    pr = prov or {"base_url": DS["base_url"].rstrip("/"), "api_key": DS["api_key"],
                  "model": DS["model"]}
    url = _norm_base(pr["base_url"]) + "/chat/completions"
    # 输出上限：未显式指定时取设置（默认 8000）
    if not max_tokens or int(max_tokens) <= 0:
        try:
            max_tokens = int(SETTINGS.get("maxOutputTokens", 8000))
        except Exception:
            max_tokens = 8000
    payload = {
        "model": model or pr["model"],
        "messages": messages,
        "stream": False,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if extra:
        payload.update(extra)   # 例如 {"thinking": {"type": "disabled"}} 关闭思考模式
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {pr['api_key']}",
            "Content-Type": "application/json",
        }
    )
    try:
        _resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        raise RuntimeError(_http_err(e, url))
    except urllib.error.URLError as e:
        raise RuntimeError(_net_err(e, url))
    with _resp as r:
        return json.loads(r.read().decode("utf-8"))

# ---- 外部引擎的默认预设：指向本机的 DSH harness（可在 config.json 的 harness 段覆盖）----
HARNESS = CONFIG.get("harness", {})
# 三项都从 config.json 的 harness 段读；不配就走「环境里能找到的 node + ~/.dsh」
HARNESS_NODE = HARNESS.get("node") or shutil.which("node") or "node"
HARNESS_BIN = os.path.normpath(HARNESS.get(
    "bin", os.path.join(DSH_HOME_DIR, "profiles", "node_modules",
                        "@deepseek-ai", "dsh", "lib", "bin.js")))
HARNESS_HOME = os.path.normpath(HARNESS.get("home", DSH_HOME_DIR))
HARNESS_CWD = os.path.normpath(HARNESS.get("cwd", os.path.join(BASE_DIR, "ws_engine")))

# ---------------------------------------------------------------------------
# 通用引擎层：一个成员可以走「接口引擎」或「外部命令引擎」
#   接口引擎（默认）：工作台内置 harness —— 读人设 → 调 OpenAI 兼容接口 → 工具循环
#   命令引擎（新增）：把话交给一条本地命令，取它 stdout 当回复 ——
#                     DSH / Claude Code / Codex / 自写脚本都能挂，两种传参风格都支持
# ---------------------------------------------------------------------------
def _split_cmd(s):
    """把用户写的一行命令拆成 argv。
    支持双引号包裹的段（内部原样保留，反斜杠不当转义符）——
    这样 Windows 上带空格的路径 "C:\\Program Files\\node.exe" 也能正确拆开。
    """
    out, cur, q = [], "", False
    for ch in str(s):
        if ch == '"':
            q = not q
        elif ch.isspace() and not q:
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _engine_of(ai_key):
    """取某个成员的工作引擎配置。默认 = 接口引擎 {"type": "api"}。
    兼容旧版：engineMode == "harness" 自动映射成命令引擎，老配置不用改。
    """
    ag = AI_CONF.get(ai_key, {})
    eng = ag.get("engine") or {}
    if str(eng.get("type") or "").lower() == "command" and eng.get("cmd"):
        return eng
    if ai_key == "agent_b" and SETTINGS.get("engineMode") == "harness":
        return {
            "type": "command",
            "cmd": [HARNESS_NODE, HARNESS_BIN, "--profile", "headless", "{prompt}"],
            "cwd": HARNESS_CWD,
            "timeout": 300,
            "env": {"DSH_HOME": HARNESS_HOME},
            "_legacy": "dsh",
        }
    return {"type": "api"}


def _engine_cmd_str(cmd):
    """argv → 一行可回填输入框的命令字符串（含空格/占位符的参数加引号，保证来回不丢）。"""
    out = []
    for c in cmd:
        c = str(c)
        out.append('"%s"' % c if (" " in c or "{" in c) else c)
    return " ".join(out)


def _engine_public(ai_key):
    """给前端的引擎描述。"""
    eng = _engine_of(ai_key)
    if str(eng.get("type") or "api") != "command":
        return {"type": "api"}
    cmd = [str(c) for c in (eng.get("cmd") or [])]
    return {
        "type": "command",
        "cmd": _engine_cmd_str(cmd),
        "cwd": eng.get("cwd") or "",
        "timeout": int(eng.get("timeout") or 300),
        "env": dict(eng.get("env") or {}),
        "stdin": not any("{prompt}" in c for c in cmd),
        "legacy": bool(eng.get("_legacy")),
    }


def _engine_from_body(raw, base=None):
    """把界面传来的 engine 描述转成 config 里存的格式。
    返回 None = 用接口引擎（即不存 engine 字段）。
    """
    if not isinstance(raw, dict):
        return None
    if str(raw.get("type") or "").strip().lower() != "command":
        return None
    cmd_raw = str(raw.get("cmd") or "").strip()
    if not cmd_raw:
        b = base or {}
        return b if str(b.get("type") or "") == "command" else None
    cmd = _split_cmd(cmd_raw)
    if not cmd:
        return None
    eng = {"type": "command", "cmd": cmd}
    cwd = str(raw.get("cwd") or "").strip()
    if cwd:
        eng["cwd"] = cwd
    try:
        to = int(float(raw.get("timeout") or 0))
    except Exception:
        to = 0
    if to > 0:
        eng["timeout"] = to
    env = raw.get("env")
    if isinstance(env, dict) and env:
        eng["env"] = {str(k): str(v) for k, v in env.items() if v}
    return eng


ENGINE_CTX_TURNS = 6   # 命令引擎是无状态单次任务，只带最近几轮上下文


def _engine_prompt(conv_messages, ai_key):
    """把最近几轮对话拼成一段纯文本，喂给外部命令。"""
    me = AI_CONF.get(ai_key, {}).get("name", ai_key)
    recent = [m for m in conv_messages
              if m.get("role") in ("user", "assistant")][-ENGINE_CTX_TURNS:]
    lines = []
    for m in recent:
        who = "用户" if m.get("role") == "user" else me
        lines.append("%s：%s" % (who, m.get("content", "")))
    return "\n".join(lines)


def run_command_engine(eng, prompt, ai_key=""):
    """通用外部命令引擎：把 prompt 交给一条命令，取 stdout 当回复。
    命令里含 {prompt} → 走命令行参数；不含 → 把 prompt 从 stdin 喂进去。
    任何失败都降级成一段说明文本，绝不抛异常打断对话。
    """
    cmd = [str(c) for c in (eng.get("cmd") or [])]
    if not cmd:
        return "[命令引擎没配好] 设置 → AI 成员与模型 → 编辑该成员 → 工作引擎，把命令填上。"
    use_arg = any("{prompt}" in c for c in cmd)
    argv = [c.replace("{prompt}", prompt) for c in cmd]
    cwd = eng.get("cwd") or BASE_DIR
    try:
        timeout = int(eng.get("timeout") or 300)
    except Exception:
        timeout = 300
    env = dict(os.environ)
    for k, v in (eng.get("env") or {}).items():
        env[str(k)] = os.path.expanduser(str(v))
    try:
        r = subprocess.run(argv, input=None if use_arg else prompt,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        return "[外部引擎超时（%ss）] 命令：%s" % (timeout, cmd[0])
    except FileNotFoundError:
        return ("[找不到外部命令] %s —— 检查是不是名字拼错了，或者它没装 / 不在 PATH 里。"
                % cmd[0])
    except Exception as e:
        return "[外部引擎执行失败：%s: %s]" % (type(e).__name__, e)
    out = (r.stdout or "").strip()
    if out:
        return out
    err = (r.stderr or "").strip()
    tail = ("；stderr 前 200 字：%s" % err[:200]) if err else ""
    return "[外部引擎没有输出] 退出码 %s%s" % (r.returncode, tail)

def _api_call_stream_full(messages, tools=None, max_tokens=None, timeout=180, model=None, prov=None):
    """
    真·流式调用。边收边 yield，不再等整段生成完：
      ('think', s)  → 思考过程（reasoning_content，模型支持时才有）
      ('text',  s)  → 正文增量
      流结束时 yield ('tools', [ {...} ]) 或 ('end', finish_reason)
    prov = 接口提供方（多协议层）；不传则用全局 DeepSeek 配置。
    """
    pr = prov or {"base_url": DS["base_url"].rstrip("/"), "api_key": DS["api_key"],
                  "model": DS["model"]}
    url = _norm_base(pr["base_url"]) + "/chat/completions"
    # 输出上限：未显式指定时取设置（默认 8000），避免大写入被 2000 砍断导致工具参数残缺
    if not max_tokens or int(max_tokens) <= 0:
        try:
            max_tokens = int(SETTINGS.get("maxOutputTokens", 8000))
        except Exception:
            max_tokens = 8000
    payload = {
        "model": model or pr["model"],
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {pr['api_key']}",
                 "Content-Type": "application/json",
                 "Accept": "text/event-stream"})
    collected = {}
    fin = None
    try:
        _resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise RuntimeError(_http_err(e, url))
    except urllib.error.URLError as e:
        raise RuntimeError(_net_err(e, url))
    with _resp as r:
        for raw in r:
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            # 最后一个 chunk 只带 usage（choices 为空），单独捕获供前端显示消耗
            _uu = chunk.get("usage")
            if isinstance(_uu, dict) and _uu.get("total_tokens"):
                yield ("usage", _uu)
            ch = (chunk.get("choices") or [{}])[0]
            d = ch.get("delta") or {}
            # 思考过程（deepseek-reasoner 等支持）
            if d.get("reasoning_content"):
                yield ("think", d["reasoning_content"])
            if d.get("content"):
                yield ("text", d["content"])
            # 工具调用在流式下是增量的，需要按 index 拼起来
            for tc in (d.get("tool_calls") or []):
                i = tc.get("index", 0)
                e = collected.setdefault(i, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    e["id"] = tc["id"]
                f = tc.get("function") or {}
                if f.get("name"):
                    e["name"] = f["name"]
                if f.get("arguments"):
                    e["arguments"] += f["arguments"]
            if ch.get("finish_reason"):
                fin = ch["finish_reason"]
    if collected:
        yield ("tools", [collected[k] for k in sorted(collected)])
    else:
        yield ("end", fin or "stop")

def run_agent(ai_key: str, conv_messages: list, enable_tools: bool = True,
              system_override: str = None):
    """
    跑一次 agent。conv_messages 是 [{role, content}]（不含 system）。
    返回 (final_text, events) ，events 是工具/状态事件列表，便于前端展示。
    system_override：用自定义 system 提示（群聊模式需要追加群聊规则）
    """
    # 命令引擎：这个成员的工作方式 = 把话交给一条外部命令（DSH / Claude Code / 自写脚本…）
    _eng = _engine_of(ai_key)
    if str(_eng.get("type") or "api") == "command":
        return run_command_engine(_eng, _engine_prompt(conv_messages, ai_key), ai_key), []

    if not _prov_ready(ai_key):
        return _no_key_msg(ai_key), []

    system = {"role": "system", "content": _system_prompt_for(ai_key, system_override)}
    messages = [system] + list(conv_messages)
    model = get_model(ai_key)
    events = []
    for _ in range(MAX_TOOL_ROUNDS):
        m_now, p_now, t_now = _wrap_before_request(
            ai_key, model, _prov(ai_key), messages, all_tools() if enable_tools else None)
        try:
            resp = _api_call(messages, t_now, model=m_now, prov=p_now)
        except Exception as e:
            return f"[调用 DeepSeek 出错：{repr(e)}]", events
        choice = resp["choices"][0]["message"]
        # 工具调用？
        tool_calls = choice.get("tool_calls")
        if tool_calls and enable_tools:
            messages.append(choice)  # 把 assistant 的 tool_calls 原样加回去
            for tc in tool_calls:
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments", "{}") or "{}")
                except Exception:
                    args = {}
                try:
                    ok, result = exec_tool(fn["name"], args, ai_key)
                except Exception as _e:
                    ok, result = False, f"⚠️ 工具执行异常：{repr(_e)}"
                ok, result = _wrap_on_tool(ai_key, fn["name"], args, ok, result)
                events.append({
                    "type": "tool",
                    "name": fn["name"],
                    "args": args,
                    "result": (result[:600] + "…") if isinstance(result, str) and len(result) > 600 else result,
                    "ok": ok,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
            continue  # 进入下一轮，让模型基于工具结果继续
        # 没有工具调用 → 这是最终回答
        return choice.get("content", "").strip(), events
    # 兜底：工具轮次耗尽仍未产出最终回答 → 强制收尾（不再给工具）
    try:
        resp = _api_call(messages, None, model=model)
        return resp["choices"][0]["message"].get("content", "").strip(), events
    except Exception as e:
        return f"[达到最大工具轮次且收尾失败：{repr(e)}]", events

def run_agent_stream(ai_key: str, conv_messages: list, enable_tools: bool = True,
                     system_override: str = None):
    """
    包装层：累计 token 消耗与步骤数，收尾补发一条 usage 汇总事件。
    真正的执行在 _run_agent_stream_raw。
    """
    _t0 = time.time()
    _u = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    _steps = 0
    _nchars = 0
    try:
        _prompt_chars = len(json.dumps(conv_messages, ensure_ascii=False))
    except Exception:
        _prompt_chars = 0
    try:
        _model = get_model(ai_key)
    except Exception:
        _model = ""
    for ev in _run_agent_stream_raw(ai_key, conv_messages, enable_tools, system_override):
        if ev.get("type") == "usage":
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                try:
                    _u[k] += int(ev.get(k) or 0)
                except Exception:
                    pass
            continue
        if ev.get("type") == "tool":
            _steps += 1
        elif ev.get("type") == "token":
            _nchars += len(ev.get("text", "") or "")
        yield ev
    if _u["total_tokens"] or _steps or _nchars or _prompt_chars:
        # provider 未返回 usage（如部分非 DeepSeek 接口）时，按字符数粗略估算，避免空白
        if not _u["total_tokens"] and (_nchars or _prompt_chars):
            _est_p = max(1, round(_prompt_chars / 1.8))
            _est_c = max(1, round(_nchars / 1.8))
            _u["prompt_tokens"] = _est_p
            _u["completion_tokens"] = _est_c
            _u["total_tokens"] = _est_p + _est_c
            _u["estimate"] = True
        _u["elapsed"] = round(time.time() - _t0, 1)
        _u["steps"] = _steps
        _u["model"] = _model
        yield {"type": "usage", **_u}


def _run_agent_stream_raw(ai_key: str, conv_messages: list, enable_tools: bool = True,
                          system_override: str = None):
    """
    实时版 agent：边跑边 yield 事件（不等全部跑完）。
    事件类型：
      {"type":"think","text":...}  思考过程
      {"type":"token","text":...}  正文增量（真流式）
      {"type":"tool", ...}         工具调用（一执行完就推送，不再憋到最后）
    """
    # 命令引擎：一次性返回（外部命令不流式）
    _eng = _engine_of(ai_key)
    if str(_eng.get("type") or "api") == "command":
        yield {"type": "token",
               "text": run_command_engine(_eng, _engine_prompt(conv_messages, ai_key), ai_key)}
        return

    if not _prov_ready(ai_key):
        yield {"type": "token", "text": _no_key_msg(ai_key)}
        return

    system = {"role": "system", "content": _system_prompt_for(ai_key, system_override)}
    messages = [system] + list(conv_messages)
    model = get_model(ai_key)
    prov = _prov(ai_key)   # 多协议层：该成员可能指向任意 OpenAI 兼容平台

    # ③ 修「断了」：正文被 token 上限截断（finish_reason=length）→ 从断点继续，
    # 把已写内容作为 assistant 前缀塞回上下文，再让模型接着写，最多续 cont_budget 次。
    try:
        cont_budget = max(0, int(SETTINGS.get("maxContinue", 2)))
    except Exception:
        cont_budget = 2
    empty_budget = 1   # 空回复（模型没产出任何字）最多轻量重试 1 次，避免气泡一片空白

    for _ in range(MAX_TOOL_ROUNDS):
        finish = None          # 本轮结束原因（stop / length / tool_calls / ...）
        got_tools = False
        tool_calls = []
        assistant_parts = []   # 本轮正文本体，用于截断后「接着写」时回灌上下文
        yielded_text = 0
        m_now, p_now, t_now = _wrap_before_request(
            ai_key, model, prov, messages, all_tools() if enable_tools else None)
        try:
            for kind, val in _api_call_stream_full(messages, t_now,
                                                   model=m_now, prov=p_now):
                if kind == "think":
                    yield {"type": "think", "text": val}
                elif kind == "text":
                    assistant_parts.append(val)
                    yielded_text += len(val)
                    yield {"type": "token", "text": val}
                elif kind == "tools":
                    tool_calls = val
                    got_tools = True
                elif kind == "end":
                    finish = val
        except Exception as e:
            yield {"type": "token", "text": f"[调用 DeepSeek 出错：{repr(e)}]"}
            return

        if got_tools and enable_tools:
            # 校验每个工具调用的参数；finish==length 时 arguments 极可能被 token 上限截断成非法 JSON
            parsed, broken = [], False
            for t in tool_calls:
                try:
                    a = json.loads(t.get("arguments") or "{}")
                    if not isinstance(a, dict):
                        a, broken = {}, True
                except Exception:
                    a, broken = {}, True
                parsed.append(a)
            # 工具参数不完整（被截断 / 非法 JSON）→ 禁止执行，明确回错，引导模型改用 append / 拆小重写
            if broken:
                truncated = (finish == "length")
                messages.append({"role": "assistant", "tool_calls": [
                    {"id": tool_calls[0]["id"], "type": "function",
                     "function": {"name": tool_calls[0]["name"],
                                  "arguments": tool_calls[0].get("arguments", "")}}]})
                reason = ("参数被输出上限（max_tokens）截断，JSON 不完整"
                          if truncated else "工具参数不是合法 JSON")
                messages.append({"role": "tool", "tool_call_id": tool_calls[0]["id"],
                                 "content": (f"⚠️ {reason}，无法执行 {tool_calls[0]['name']}。"
                                             f"请把写入内容拆小（单次建议 < 4000 字符），"
                                             f"或对同一文件改用 write_file 的 mode=\"append\" 分块追加；"
                                             f"不要重复已写过的内容。")})
                if cont_budget > 0:
                    cont_budget -= 1
                    continue
                break  # 预算用尽 → 跳到下方纯文本强制收尾，避免死循环
            # 参数完好 → 正常执行
            messages.append({"role": "assistant", "tool_calls": [
                {"id": t["id"], "type": "function",
                 "function": {"name": t["name"], "arguments": t["arguments"]}}
                for t in tool_calls]})
            for t, a in zip(tool_calls, parsed):
                _t_tool = time.time()
                try:
                    ok, result = exec_tool(t["name"], a, ai_key)
                except Exception as _e:
                    ok, result = False, f"⚠️ 工具执行异常：{repr(_e)}"
                ok, result = _wrap_on_tool(ai_key, t["name"], a, ok, result)
                _ev = {"type": "tool", "name": t["name"], "args": a,
                       "dur": round(time.time() - _t_tool, 2),
                       "result": (result[:600] + "…") if isinstance(result, str) and len(result) > 600 else result,
                       "ok": ok}
                _fs = _files_from_tool(t["name"], a, ok)
                if _fs:
                    _ev["files"] = _fs
                yield _ev
                messages.append({"role": "tool", "tool_call_id": t["id"], "content": result})
            continue
        # 截断：把已写部分回灌，再让模型续写（同一气泡，前端无感）
        if finish == "length" and cont_budget > 0:
            cont_budget -= 1
            if assistant_parts:
                messages.append({"role": "assistant",
                                  "content": "".join(assistant_parts)})
            messages.append({"role": "user",
                              "content": "（你的回复被截断了，请从中断处继续输出剩余内容，"
                                         "不要重复已经写过部分。）"})
            continue
        # 空回复：轻量重试一次，避免气泡空白
        if yielded_text == 0 and empty_budget > 0:
            empty_budget -= 1
            messages.append({"role": "user", "content": "（请给出你的回答。）"})
            continue
        return  # 正常结束（stop 且有内容）

    # 轮次耗尽 → 强制收尾（不再给工具）
    try:
        for kind, val in _api_call_stream_full(messages, None, model=model, prov=prov):
            if kind == "think":
                yield {"type": "think", "text": val}
            elif kind == "text":
                yield {"type": "token", "text": val}
    except Exception as e:
        yield {"type": "token", "text": f"[收尾失败：{repr(e)}]"}

# ---------------------------------------------------------------------------
# 5. Session 持久化（长期记录）
# ---------------------------------------------------------------------------
_lock = threading.Lock()

def session_paths(sid):
    return (
        os.path.join(SESSIONS_DIR, f"{sid}.json"),
        os.path.join(SESSIONS_DIR, f"{sid}.md"),
    )

def load_session(sid):
    jp, _ = session_paths(sid)
    if os.path.isfile(jp):
        try:
            with open(jp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "id": sid,
        "created": _now(),
        "agent_a": [],   # 单聊历史（键名 = 成员 key）
        "agent_b": [],   # 单聊历史（键名 = 成员 key）
        "duet": [],      # 互聊记录 [{role, content}]
        "group": [],     # 群聊记录 [{role: user|成员key, content}]
        "log": [],       # 人类可读日志 [{t, who, text}]
    }

def _session_reply(content, think_buf=None, tools_buf=None, files_buf=None):
    """组装一条要落库的 AI 回复。

    除了 content 之外，顺带把「思考过程 / 工具调用 / 产出文件」也带上 ——
    它们让界面在刷新后能原样还原，而 _mm_prepare / build_group_conv 会在
    发给模型前把它们剥掉。
    """
    rec = {"role": "assistant", "content": content or ""}
    _think = "".join(think_buf or []).strip()
    if _think:
        rec["think"] = _think
    if tools_buf:
        rec["tools"] = tools_buf
    if files_buf:
        rec["files"] = files_buf
    return rec


def save_session(sid, data):
    data["updated"] = _now()
    jp, mp = session_paths(sid)
    with _lock:
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 人类可读 md 日志
        lines = [f"# 会话 {sid}", f"- 创建：{data.get('created','')}", ""]
        for e in data.get("log", []):
            lines.append(f"**[{e['t']}] {e['who']}**：{e['text']}")
        with open(mp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

def log_and_save(sid, data, who, text):
    data.setdefault("log", []).append({"t": _now(), "who": who, "text": text})
    save_session(sid, data)
    sync_to_external(sid, who, text)

# ---------------------------------------------------------------------------
# 5b. 把工作台里发生的事同步到一个外部目录（可选功能）
#     在 config.json 里配了 sync_dir 才会启用，不配就是关闭状态。
#     用途：把对话流水导出给另一个程序 / 另一个 AI 读取。
# ---------------------------------------------------------------------------
SYNC_DIR = os.path.normpath(CONFIG.get("sync_dir", os.path.join(BASE_DIR, "shared_sync")))
SYNC_PREFIX = CONFIG.get("sync_prefix", "SYNC_")
SYNC_ENABLED = bool(CONFIG.get("sync_to_external", bool(CONFIG.get("sync_dir"))))

def sync_to_external(sid, who, text, kind="对话"):
    if not SYNC_ENABLED:
        return
    try:
        os.makedirs(SYNC_DIR, exist_ok=True)
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        fp = os.path.join(SYNC_DIR, f"{SYNC_PREFIX}工作台同步_{today}.md")
        body = (text or "").replace("\n", " ")[:500]
        line = f"- [{_now()}] [{kind}] [{who}] (会话 {sid}) {body}"
        with _lock:
            with open(fp, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            # 最新动态：覆盖式单值文件，一眼看到最近进展
            latest = os.path.join(SYNC_DIR, f"{SYNC_PREFIX}工作台最新动态.md")
            with open(latest, "w", encoding="utf-8") as f:
                f.write(
                    "# 工作台最新动态（工作台自动生成）\n\n"
                    f"- 更新时间：{_now()}\n"
                    f"- 当前会话：{sid}\n"
                    f"- 最后一条：[{who}] {body[:300]}\n"
                    f"- 今日流水：{SYNC_PREFIX}工作台同步_{today}.md\n"
                )
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 6. SSE 工具
# ---------------------------------------------------------------------------
def sse(event_dict):
    # 防「毒文本」：模型偶尔吐出代理字符（\ud83d 这类），ensure_ascii=False 时会
    # 原样进 JSON，等 StreamingResponse 编码 UTF-8 才炸 → 整条流无声断掉。
    # 这里提前试编码，不干净就退回全转义模式，保证发给前端的字节永远合法。
    try:
        body = json.dumps(event_dict, ensure_ascii=False)
        body.encode("utf-8")
    except Exception:
        body = json.dumps(event_dict, ensure_ascii=True)
    return f"data: {body}\n\n"

def chunk_text(text, size=10):
    """把文本切成小块，制造打字机流式效果。中文按字符切。"""
    out = []
    i = 0
    n = len(text)
    while i < n:
        out.append(text[i:i+size])
        i += size
    return out

# ---------------------------------------------------------------------------
# 7. FastAPI 应用
# ---------------------------------------------------------------------------
app = FastAPI(title="多 AI 工作台")

@app.get("/")
def index():
    resp = FileResponse(os.path.join(BASE_DIR, "static", "index.html"))
    # 关缓存：换版本后刷新页面一定能拿到新的，不会被浏览器糊住旧界面
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.get("/api/sessions")
def list_sessions():
    """列出所有历史会话（含消息数、更新时间），供前端回看聊天记录"""
    items = []
    for fn in os.listdir(SESSIONS_DIR):
        if not fn.endswith(".json"):
            continue
        sid = fn[:-5]
        jp = os.path.join(SESSIONS_DIR, fn)
        try:
            with open(jp, "r", encoding="utf-8") as f:
                d = json.load(f)
            n = sum(len(v) for k, v in d.items()
                    if isinstance(v, list) and k != "log")
            items.append({"id": sid, "msgs": n,
                          "created": d.get("created", ""),
                          "updated": d.get("updated", d.get("created", ""))})
        except Exception:
            items.append({"id": sid, "msgs": 0, "created": "", "updated": ""})
    items.sort(key=lambda x: (x.get("updated") or x["id"]), reverse=True)
    return JSONResponse({"sessions": items})

@app.get("/api/session/{sid}")
def get_session(sid):
    """读取某个会话的完整历史，前端据此回显聊天记录"""
    return JSONResponse(load_session(sid))

@app.post("/api/reset")
async def reset_session(request: Request):
    body = await request.json()
    sid = body.get("session_id", "default")
    data = load_session(sid)
    # 清空所有消息列表（兼容任意成员：单聊键、duet、group 都一并清）
    for k, v in list(data.items()):
        if isinstance(v, list):
            data[k] = []
    save_session(sid, data)
    return JSONResponse({"ok": True})

# ---------------------------------------------------------------------------
# 6b. 用户设置 & 外部引擎(dsh)插件管理
# ---------------------------------------------------------------------------
@app.get("/api/settings")
def get_settings():
    # 插件钩子：settings 允许插件改写「前端读到的设置」（比如强制主题、锁死字号）
    hp = run_hooks("settings", {"settings": dict(SETTINGS)})
    return JSONResponse(hp.get("settings", SETTINGS))

@app.get("/api/models")
def get_models():
    return JSONResponse({
        "catalog": MODEL_CATALOG,
        "default": DS.get("model", "deepseek-flash"),
    })

@app.post("/api/settings")
async def post_settings(request: Request):
    body = await request.json()
    return JSONResponse(save_settings(body))


# ---------------------------------------------------------------------------
# 服务商预设：界面里做成下拉，省得用户自己猜地址和模型名
#   模型名必须填服务商的「真实 ID」——写 K2.6、kimi-k2 这类都会被服务端拒掉，
#   所以这里把常用几家连地址带模型一起列出来，选一下就自动填好。
# ---------------------------------------------------------------------------
PROVIDER_PRESETS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "models": ["deepseek-flash", "deepseek-v4-pro"],
     "key_url": "https://platform.deepseek.com/api_keys",
     "note": "国内直连，工作台默认就它"},
    {"id": "moonshot", "name": "Kimi / Moonshot", "base_url": "https://api.moonshot.cn/v1",
     "models": ["kimi-k2.6", "kimi-k2.5", "kimi-k3", "kimi-k2.7-code"],
     "key_url": "https://platform.kimi.com",
     "note": "国内站。国际站地址是 api.moonshot.ai，两边 Key 不通用"},
    {"id": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "models": ["glm-4.6", "glm-4-flash"],
     "key_url": "https://open.bigmodel.cn",
     "note": "glm-4-flash 有免费额度，适合先试水"},
    {"id": "qwen", "name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
     "key_url": "https://bailian.console.aliyun.com",
     "note": "地址里的 compatible-mode 这一段不能省"},
    {"id": "volc", "name": "豆包 / 火山方舟", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
     "models": [],
     "key_url": "https://console.volcengine.com/ark",
     "note": "模型名要填控制台里的「接入点 ID」（ep- 开头），填模型名会失败"},
    {"id": "hunyuan", "name": "腾讯混元", "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
     "models": ["hunyuan-turbos-latest", "hunyuan-turbo-latest", "hunyuan-standard"],
     "key_url": "https://console.cloud.tencent.com/hunyuan",
     "note": "具体模型名以控制台可用列表为准"},
    {"id": "siliconflow", "name": "硅基流动 SiliconFlow", "base_url": "https://api.siliconflow.cn/v1",
     "models": [],
     "key_url": "https://cloud.siliconflow.cn",
     "note": "聚合多家模型，国内可直连，有免费额度"},
    {"id": "openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
     "models": [],
     "key_url": "https://openrouter.ai/keys",
     "note": "模型最全（含 Claude / GPT / Gemini），美元计价"},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1",
     "models": ["gpt-5.5", "gpt-5.4-mini"],
     "key_url": "https://platform.openai.com/api-keys",
     "note": "国内使用需要自备网络条件"},
    {"id": "gemini", "name": "Google Gemini",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "models": ["gemini-3.6-flash", "gemini-3.6-pro"],
     "key_url": "https://aistudio.google.com/apikey",
     "note": "走 OpenAI 兼容端点，地址比较长，照抄别改"},
    {"id": "ollama", "name": "本地 Ollama", "base_url": "http://localhost:11434/v1",
     "models": [],
     "key_url": "",
     "note": "完全离线零成本。Key 随便填几个字符，但不能留空"},
]


@app.get("/api/provider-presets")
def get_provider_presets():
    """给界面下拉用：服务商地址 + 常用模型名 + 申请 Key 的入口。"""
    return JSONResponse({"presets": PROVIDER_PRESETS})


@app.post("/api/provider-models")
async def fetch_provider_models(request: Request):
    """拿填好的 base_url + key 去问服务商要「可用模型列表」（GET {base}/models）。
    模型名拿不准时点一下，让服务商自己报名字，比自己猜靠谱。"""
    body = await request.json()
    base = _norm_base(body.get("base_url"))
    key = str(body.get("api_key") or "").strip()
    if not base:
        return JSONResponse({"ok": False, "error": "先填接口地址"})
    if not key:
        return JSONResponse({"ok": False, "error": "先填 API Key（没保存也行，临时用一下）"})
    url = base + "/models"
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        ids = sorted(str(m.get("id")) for m in (data.get("data") or [])
                     if isinstance(m, dict) and m.get("id"))
        return JSONResponse({"ok": True, "count": len(ids),
                             "models": ids[:200], "url": url})
    except urllib.error.HTTPError as e:
        return JSONResponse({"ok": False, "url": url, "error": _http_err(e, url)})
    except urllib.error.URLError as e:
        return JSONResponse({"ok": False, "url": url, "error": _net_err(e, url)})
    except Exception as e:
        return JSONResponse({"ok": False, "url": url,
                             "error": "%s: %s" % (type(e).__name__, e)})


@app.get("/api/provider")
def get_provider():
    """全局默认接口（成员没有单独配 provider 时用它）。

    api_key 回明文：前端要用它回填输入框（type=password 掩码显示）。
    早先只回打码，用户重新打开设置看到空框，以为 Key 丢了 —— 值其实还在，
    但「看不见就等于没有」，反而诱导他重填甚至覆盖。本机个人工具，
    浏览器与 config.json 同机，故取可用性。
    """
    k = DS.get("api_key") or ""
    return JSONResponse({
        "base_url": DS.get("base_url", ""),
        "model": DS.get("model", ""),
        "api_key": k,
        "has_key": bool(k),
    })


@app.post("/api/provider")
async def set_provider(request: Request):
    """改全局默认接口。api_key 传 "-" 表示清空；不传表示不改。"""
    body = await request.json()
    if "base_url" in body:
        DS["base_url"] = str(body.get("base_url") or "").strip() or \
            FALLBACK_CONFIG["deepseek"]["base_url"]
    if "model" in body:
        DS["model"] = str(body.get("model") or "").strip() or "deepseek-flash"
    ak = body.get("api_key")
    if ak is not None:
        ak = str(ak).strip()
        DS["api_key"] = "" if ak == "-" else ak
    _save_config()
    return JSONResponse({"ok": True, "has_key": bool(DS.get("api_key"))})


# ---------------------------------------------------------------------------
# 文件服务：让 AI 写出来的文件能在网页里直接预览 / 下载
#   以前 write_file 只回一行「已写入：路径」，用户得自己拿路径去磁盘翻。
#   现在挂一个 HTTP 入口：默认按原类型返回（图片可直接 <img src>），
#   text=1 返回文本内容，dl=1 强制下载。
#   安全：只允许「成员工作区 + 上载目录」内的文件，路径必须能规范到这些根之下。
# ---------------------------------------------------------------------------
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
except Exception:
    pass

# 可直接当文本预览的扩展名；其余按二进制返回（浏览器能显示的就显示，不能的就下载）
TEXT_PREVIEW_EXT = {".md", ".txt", ".json", ".py", ".js", ".ts", ".css", ".html",
                    ".yaml", ".yml", ".ini", ".cfg", ".log", ".csv", ".sh", ".bat",
                    ".xml", ".sql", ".toml", ".gitignore", ".env", ".srt", ".vtt"}
MAX_PREVIEW_CHARS = 200000


def _file_roots():
    """允许对外提供文件服务的根目录（绝对路径列表）。"""
    roots = []
    for _k, _ag in AI_CONF.items():
        ws = _ag.get("workspace")
        if ws:
            try:
                roots.append(os.path.normpath(os.path.abspath(ws)))
            except Exception:
                pass
    roots.append(os.path.normpath(os.path.abspath(UPLOAD_DIR)))
    seen, out = set(), []
    for r in roots:
        if r.lower() not in seen:
            seen.add(r.lower())
            out.append(r)
    return out


def _file_allowed(absp):
    """该文件是否落在允许的根目录内 —— 不在返回 None。

    额外放行「本会话 AI 自己写出来并登记过的文件」：AI 可以把文件写到安装
    目录、桌面等地方，那些路径不在工作区白名单里，但既然是自己产出的，
    用户就应当能直接预览/下载。只认登记表里的精确路径，不做目录级放宽，
    所以不会顺带把 config.json 这类文件暴露出去。
    """
    for r in _file_roots():
        if absp == r or absp.startswith(r + os.sep):
            return r
    try:
        if os.path.abspath(absp) in _produced_paths():
            return os.path.dirname(os.path.abspath(absp))
    except Exception:
        pass
    return None


def _files_from_tool(name, args, ok):
    """从工具调用里提取「涉及的文件」附件，供前端渲染文件卡片。"""
    if not ok:
        return []
    paths = []
    if name in ("write_file", "read_file"):
        paths = [args.get("path")]
    out = []
    for p in paths:
        if not p:
            continue
        try:
            absp = os.path.normpath(os.path.abspath(p))
        except Exception:
            continue
        try:
            size = os.stat(absp).st_size
            missing = False
        except Exception:
            size, missing = 0, True
        out.append({"name": os.path.basename(absp), "path": absp, "size": size,
                    "missing": missing,
                    "served": bool(_file_allowed(absp)) and not missing})
    return out


# ---------------------------------------------------------------------------
# 会话产出文件登记表
#   AI 用 write_file 写出来的文件，写在安装目录 / 桌面 / 工作区……都记下来：
#   · 前端「产出文件」抽屉里单列一组「本会话产出」，写在哪儿都看得到；
#   · /api/file 对登记过的精确路径额外放行，能预览能下载。
#   记录落在 BASE_DIR/.session_files.json（跨重启保留）。
# ---------------------------------------------------------------------------
_SESS_FILES_PATH = os.path.join(BASE_DIR, ".session_files.json")
_SESS_FILES = {}


def _load_session_files():
    global _SESS_FILES
    try:
        with open(_SESS_FILES_PATH, encoding="utf-8") as f:
            d = json.load(f)
        _SESS_FILES = d if isinstance(d, dict) else {}
    except Exception:
        _SESS_FILES = {}


def _remember_session_files(sid, files):
    """把「本次会话产出的文件」登记进表（去重、跨重启保留）。"""
    if not files:
        return
    try:
        with _lock:
            cur = _SESS_FILES.setdefault(str(sid), [])
            seen = {x.get("path") for x in cur if isinstance(x, dict)}
            changed = False
            for f in files:
                p = (f or {}).get("path")
                if not p or p in seen:
                    continue
                seen.add(p)
                cur.append({"path": p,
                            "name": f.get("name") or os.path.basename(p),
                            "size": f.get("size"),
                            "t": _now()})
                changed = True
            if changed:
                with open(_SESS_FILES_PATH, "w", encoding="utf-8") as fh:
                    json.dump(_SESS_FILES, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _produced_paths():
    """登记表里出现过的全部绝对路径（供 /api/file 放行判断）。"""
    out = set()
    try:
        for v in _SESS_FILES.values():
            for x in (v or []):
                p = (x or {}).get("path")
                if p:
                    out.add(os.path.abspath(p))
    except Exception:
        pass
    return out


_load_session_files()


@app.get("/api/session-files")
def api_session_files(sid: str = ""):
    """本会话 AI 写出来的文件（写在安装目录、桌面也能列出来）。"""
    items = list(_SESS_FILES.get(sid) or [])
    out = []
    for it in items:
        p = (it or {}).get("path") or ""
        try:
            st = os.stat(p)
            size, mtime, exists = st.st_size, int(st.st_mtime), True
        except Exception:
            size, mtime, exists = int(it.get("size") or 0), 0, False
        out.append({"name": it.get("name") or os.path.basename(p), "path": p,
                    "size": size, "mtime": mtime,
                    "served": exists, "missing": not exists,
                    "t": it.get("t", "")})
    out.reverse()          # 最近的排前面
    return JSONResponse({"ok": True, "sid": sid, "files": out[:300]})


@app.get("/api/ws-files")
def api_ws_files(ai: str = ""):
    """
    列出某成员工作区（与上载目录）的文件，供前端「产出文件」侧栏使用。
    只返回允许访问的目录内内容，跳过隐藏文件与常见噪音目录。
    """
    roots = []
    ag = (AI_CONF.get(ai) or {}) if ai else {}
    if ag.get("workspace"):
        roots.append(ag["workspace"])
    up = os.path.join(BASE_DIR, "uploads")
    if os.path.isdir(up):
        roots.append(up)
    if not roots:
        for _k, _a in (AI_CONF or {}).items():
            if _a.get("workspace") and os.path.isdir(_a["workspace"]):
                roots.append(_a["workspace"])
    SKIP = {".git", "node_modules", "__pycache__", "venv", ".venv", ".idea", ".vscode"}
    out = []
    seen = set()
    for root in roots:
        try:
            root_abs = os.path.abspath(root)
        except Exception:
            continue
        for dirpath, dirnames, filenames in os.walk(root_abs, followlinks=False):
            # 防「特殊目录」卡死扫描：junction/软链不深入（防目录环）、深度封顶 3 层、
            # 总条目封顶 800。以前一个链接环能把 /api/ws-files 挂死，抽屉永远转圈。
            depth = 0 if dirpath == root_abs else dirpath[len(root_abs):].count(os.sep)
            _clean = []
            for _d in dirnames:
                if _d in SKIP or _d.startswith("."):
                    continue
                try:
                    _full = os.path.join(dirpath, _d)
                    if os.path.realpath(_full) != os.path.normpath(_full):
                        continue        # 指向别处的 junction / 软链接 → 不深入
                except Exception:
                    pass
                _clean.append(_d)
            dirnames[:] = _clean
            if depth >= 3:
                dirnames[:] = []
            if len(out) >= 800:
                break
            for fn in filenames:
                if len(out) >= 800:
                    break
                if fn.startswith("."):
                    continue
                fp = os.path.join(dirpath, fn)
                if fp in seen:
                    continue
                seen.add(fp)
                if not _file_allowed(fp):
                    continue
                try:
                    st = os.stat(fp)
                except Exception:
                    continue
                rel = os.path.relpath(fp, root_abs).replace("\\", "/")
                out.append({
                    "name": fn, "path": fp, "rel": rel, "root": root_abs,
                    "root_name": os.path.basename(root_abs),
                    "size": st.st_size, "mtime": int(st.st_mtime), "served": True,
                })
    out.sort(key=lambda x: (x["root_name"], x["rel"]))
    return JSONResponse({"ok": True, "ai": ai, "files": out[:400], "total": len(out)})


@app.get("/api/file")
def api_file(path: str = "", dl: str = "", text: str = ""):
    """读工作区里的文件：默认按原类型返回；text=1 返回文本内容；dl=1 强制下载。"""
    import mimetypes
    if not path:
        return JSONResponse({"error": "缺少 path 参数"}, status_code=400)
    try:
        absp = os.path.normpath(os.path.abspath(path))
    except Exception:
        return JSONResponse({"error": "路径不合法"}, status_code=400)
    if not _file_allowed(absp):
        return JSONResponse(
            {"error": "该文件不在允许的目录内（只允许各成员工作区与上载目录）"},
            status_code=403)
    if not os.path.isfile(absp):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    ext = os.path.splitext(absp)[1].lower()
    name = os.path.basename(absp)
    mt = mimetypes.guess_type(absp)[0] or "application/octet-stream"
    if text == "1" or (ext in TEXT_PREVIEW_EXT and dl != "1" and text != "0"):
        try:
            with open(absp, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception as e:
            return JSONResponse({"error": f"读取失败：{e}"}, status_code=500)
        return JSONResponse({"ok": True, "name": name, "path": absp, "kind": "text",
                             "size": os.path.getsize(absp), "ext": ext,
                             "truncated": len(content) > MAX_PREVIEW_CHARS,
                             "content": content[:MAX_PREVIEW_CHARS]})
    headers = {}
    if dl == "1":
        try:
            name.encode("ascii")
            headers["Content-Disposition"] = f'attachment; filename="{name}"'
        except UnicodeEncodeError:
            from urllib.parse import quote
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    else:
        headers["Content-Disposition"] = "inline"
    return FileResponse(absp, media_type=mt, headers=headers)


# ---------------------------------------------------------------------------
# 聊天附件：用户可以把文件 / 图片发给 AI
#   图片 -> 编成 OpenAI 多模态 content（原图直发）；文本类 -> 内容并进正文；
#   其余（压缩包/音视频/xlsx…）-> 只给路径，让 AI 用 read_file 自己去看
# ---------------------------------------------------------------------------
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024        # 单个上传上限 20MB
MAX_ATTACH_COUNT = 12                      # 一条消息最多带几个附件
MAX_IMAGE_BYTES = 8 * 1024 * 1024          # 超过这个尺寸的图不再内联（避免请求爆掉）
MAX_ATTACH_TEXT_BYTES = 200 * 1024         # 文本类附件内联上限
MAX_ATTACH_IMAGES = 8                      # 一次最多内联几张图


def _att_meta(attachments):
    """规范附件元信息：只留「确实存在且在允许目录内」的那些。
    会话历史里也只存这份元信息 —— 文件内容不进 session json。"""
    out = []
    for a in (attachments or [])[:MAX_ATTACH_COUNT]:
        if not isinstance(a, dict):
            continue
        p = a.get("path") or ""
        try:
            absp = os.path.normpath(os.path.abspath(p)) if p else ""
        except Exception:
            continue
        if not absp or not os.path.isfile(absp) or not _file_allowed(absp):
            continue
        ext = os.path.splitext(absp)[1].lower()
        out.append({"name": os.path.basename(absp), "path": absp,
                    "size": os.path.getsize(absp),
                    "kind": ("image" if ext in IMG_EXT
                             else ("text" if ext in TEXT_PREVIEW_EXT else "file"))})
    return out


def _user_content(text, attachments):
    """把「文字 + 附件」拼成给模型的 content —— 纯文本 str，或多模态 list。"""
    import base64
    import mimetypes
    metas = _att_meta(attachments)
    if not metas:
        return (text or "").strip() or "（用户没有说话，只发了附件）"
    blocks, notes = [], []
    img_used = 0
    for m in metas:
        ext = os.path.splitext(m["path"])[1].lower()
        if m["kind"] == "image" and m["size"] <= MAX_IMAGE_BYTES and img_used < MAX_ATTACH_IMAGES:
            try:
                with open(m["path"], "rb") as fh:
                    raw = fh.read()
                mt = mimetypes.guess_type(m["path"])[0] or "image/png"
                blocks.append({"type": "image_url",
                               "image_url": {"url": "data:%s;base64,%s"
                                                    % (mt, base64.b64encode(raw).decode("ascii"))}})
                img_used += 1
                continue
            except Exception:
                pass
        if m["kind"] == "text" and m["size"] <= MAX_ATTACH_TEXT_BYTES:
            try:
                with open(m["path"], "r", encoding="utf-8", errors="replace") as fh:
                    body_txt = fh.read()
                notes.append("\n\n【附件：%s】\n%s" % (m["name"], body_txt))
                continue
            except Exception:
                pass
        too_big = m["size"] > MAX_IMAGE_BYTES
        notes.append("\n\n【附件：%s（%d 字节%s）】完整路径：%s —— 想看内容请用 read_file 工具打开。"
                     % (m["name"], m["size"],
                        "，过大未内联" if too_big else "", m["path"]))
    head = ((text or "").strip() + "".join(notes)).strip()
    if not blocks:
        return head or "（用户没有说话，只发了附件）"
    return ([{"type": "text", "text": head}] if head else []) + blocks


def _mm_one(h):
    """把一条会话记录转成给模型看的样子（带附件就变多模态）。"""
    c = h.get("content") or ""
    if h.get("attachments"):
        return {"role": h.get("role"), "content": _user_content(c, h.get("attachments"))}
    return {"role": h.get("role"), "content": c}


def _mm_prepare(messages):
    """整个会话列表批量过一遍 _mm_one，顺手剥掉「只给界面看」的字段。

    think / tools / files 是为了「刷新后还能还原思考过程与文件卡片」才落进
    会话文件的，模型不认识这几个键 —— 原样透传会被部分服务商直接拒绝，
    所以发请求前统一清洗成 {role, content}。
    """
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if m.get("role") == "user" and m.get("attachments"):
            out.append(_mm_one(m))
            continue
        clean = {"role": m.get("role"), "content": m.get("content") or ""}
        for k in ("tool_calls", "tool_call_id", "name"):
            if k in m:
                clean[k] = m[k]
        out.append(clean)
    return out


@app.post("/api/upload")
async def api_upload(request: Request):
    """聊天附件上传：前端把文件读成 base64 后 POST 过来，落盘到 uploads/ 并返回元信息。"""
    import base64
    import re as _re
    body = await request.json()
    raw_name = (body.get("name") or "").strip() or ("file_%d" % int(time.time()))
    b64 = body.get("content") or ""
    try:
        raw = base64.b64decode(b64.split(",")[-1])
    except Exception:
        return JSONResponse({"error": "内容解码失败"}, status_code=400)
    if not raw:
        return JSONResponse({"error": "文件内容为空"}, status_code=400)
    if len(raw) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"error": "文件过大：%.1fMB（上限 %dMB）"
                      % (len(raw) / 1048576.0, MAX_UPLOAD_BYTES // 1048576)},
            status_code=413)
    # 文件名消毒：只留文件名，非法字符替换掉，防止写穿目录
    safe = os.path.basename(raw_name)
    safe = _re.sub(r'^[\s.]+', "", safe)
    safe = _re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", safe) or "file"
    if len(safe) > 120:
        stem, ext = os.path.splitext(safe)
        safe = stem[:120 - len(ext)] + ext
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    target = os.path.join(UPLOAD_DIR, safe)
    if os.path.exists(target):
        stem, ext = os.path.splitext(safe)
        n = 1
        while os.path.exists(os.path.join(UPLOAD_DIR, "%s_%d%s" % (stem, n, ext))):
            n += 1
        target = os.path.join(UPLOAD_DIR, "%s_%d%s" % (stem, n, ext))
    try:
        with open(target, "wb") as fh:
            fh.write(raw)
    except Exception as e:
        return JSONResponse({"error": "写入失败：%s" % e}, status_code=500)
    ext = os.path.splitext(target)[1].lower()
    return JSONResponse({"ok": True, "name": os.path.basename(target), "path": target,
                         "size": len(raw), "ext": ext,
                         "kind": ("image" if ext in IMG_EXT
                                  else ("text" if ext in TEXT_PREVIEW_EXT else "file"))})


@app.get("/api/setup")
def api_setup():
    """给前端的「首次使用检查」：有几个成员、默认接口配好没、每个成员就绪没。"""
    return JSONResponse({
        "agents": len(AI_CONF),
        "config_file": os.path.isfile(CONFIG_PATH),
        "default": {
            "base_url": DS.get("base_url", ""),
            "model": DS.get("model", ""),
            "has_key": bool(DS.get("api_key")),
        },
        "agent_ready": {k: _prov_ready(k) for k in AI_CONF},
        "any_ready": any(_prov_ready(k) for k in AI_CONF),
    })

# ---------------------------------------------------------------------------
# 6c. AI 成员管理（多 AI 工作台核心）：增 / 删 / 查，写回 config.json
# ---------------------------------------------------------------------------
def _save_config():
    """写回 config.json，写前备份 config.json.bak"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            old = f.read()
        with open(CONFIG_PATH + ".bak", "w", encoding="utf-8") as f:
            f.write(old)
    except Exception:
        pass
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)

def _reload_agents():
    """成员变更后：重建注册表 / 系统提示，建好新成员的工作区目录"""
    global AI_CONF, AGENT_ORDER
    AI_CONF = _load_agents()
    AGENT_ORDER = list(AI_CONF.keys())
    for ai in AI_CONF.values():
        os.makedirs(ai["workspace"], exist_ok=True)
        os.makedirs(os.path.join(ai["workspace"], "memory"), exist_ok=True)
    rebuild_prompts()

@app.get("/api/agents")
def list_agents():
    def _mask(k):
        """key 打码：只露前 6 位，避免整个 key 明文挂在接口里"""
        return (k[:6] + "…") if k else ""
    items = [
        {"key": k, "name": a["name"], "emoji": a.get("emoji", ""),
         "color": a.get("color"), "prefix": a.get("prefix"),
         "workspace": a.get("workspace"), "model": get_model(k),
         "personas": len(a.get("persona_files", [])),
         "persona_files": a.get("persona_files", []),
         # provider 的 key 回「明文 + 打码」两份：
         #   明文给「编辑成员」弹窗回填（输入框是 type=password，视觉上仍是圆点），
         #   打码给成员卡片列表显示。
         # ⚠ 只回打码会出大事：前端把打码串当真实值回填，用户一点保存就把
         #   Key 覆盖成 "sk-abc…"，等于悄悄毁掉配置。
         "provider": ({
             "api_key_masked": _mask((a.get("provider") or {}).get("api_key") or ""),
             **{k2: v for k2, v in (a.get("provider") or {}).items() if v},
         } if (a.get("provider") or {}) else None),
         "engine": _engine_public(k)}
        for k, a in AI_CONF.items()]
    # 插件钩子：agents 可改成员显示（改名、换色、藏起来、加自定义字段）
    hp = run_hooks("agents", {"agents": items})
    return JSONResponse({"agents": hp.get("agents", items)})

@app.post("/api/agents")
async def modify_agents(request: Request):
    body = await request.json()
    action = body.get("action")

    if action == "add":
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "名字不能为空"}, status_code=400)
        key = (body.get("key") or "").strip() or f"ai{int(time.time() * 1000) % 100000}"
        if key in AI_CONF:
            return JSONResponse({"error": f"标识 {key} 已存在"}, status_code=400)
        personas = [p.strip() for p in (body.get("personas") or "").splitlines() if p.strip()]
        # 多协议层：可选 provider（OpenAI 兼容接口），让这个成员走别的平台（GPT/Kimi/GLM…）
        prov_in = body.get("provider") or {}
        prov = {k2: prov_in[k2].strip() for k2 in ("base_url", "api_key", "model")
                if prov_in.get(k2) and str(prov_in[k2]).strip()}
        ag = {
            "key": key, "name": name,
            "emoji": (body.get("emoji") or "🤖").strip() or "🤖",
            "color": body.get("color") or DEFAULT_COLORS[len(AI_CONF) % len(DEFAULT_COLORS)],
            "prefix": (body.get("prefix") or key[:2].upper() + "_").strip(),
            "workspace": (body.get("workspace") or "").strip() or os.path.join(BASE_DIR, "ws_" + key),
            "persona_files": personas,
            "model": body.get("model") or "deepseek-flash",
            "trusted": False,
        }
        if prov:
            ag["provider"] = prov
        _eng_in = _engine_from_body(body.get("engine"))
        if _eng_in:
            ag["engine"] = _eng_in
        CONFIG.setdefault("agents", []).append(ag)
        _save_config()
        _reload_agents()
        return JSONResponse({"ok": True, "key": key})

    if action == "delete":
        key = body.get("key")
        if key not in AI_CONF:
            return JSONResponse({"error": "成员不存在"}, status_code=404)
        CONFIG["agents"] = [a for a in CONFIG.get("agents", []) if a.get("key") != key]
        _save_config()
        _reload_agents()
        # 顺手清掉设置里它的模型选择和群聊身份
        SETTINGS.pop(f"{key}_model", None)
        if isinstance(SETTINGS.get("groupRoles"), dict):
            SETTINGS["groupRoles"].pop(key, None)
        save_settings(SETTINGS)
        return JSONResponse({"ok": True})

    if action == "update":
        key = body.get("key")
        if key not in AI_CONF:
            return JSONResponse({"error": "成员不存在"}, status_code=404)
        ag = next((a for a in CONFIG.get("agents", []) if a.get("key") == key), None)
        if ag is None:
            return JSONResponse({"error": "该成员来自旧配置段落，请在 config.json 里把它挪进 agents 数组后再编辑"},
                                 status_code=400)
        for f in ("name", "emoji", "color", "prefix", "workspace"):
            if body.get(f):
                ag[f] = str(body[f]).strip()
        if "personas" in body:
            ag["persona_files"] = [p.strip() for p in (body.get("personas") or "").splitlines() if p.strip()]
        # 多协议层：可整体替换/清除 provider（传 {} 表示清掉，回到全局 DeepSeek）
        if isinstance(body.get("provider"), dict):
            # 按字段合并：只传了哪个字段就改哪个，其余保留。
            # 值传 "-" = 清除这一项；全部清空 = 该成员回到「默认接口」。
            prov_in = body["provider"]
            cur = dict(ag.get("provider") or {})
            for k2 in ("base_url", "api_key", "model"):
                if k2 not in prov_in:
                    continue
                v = str(prov_in[k2]).strip()
                if v == "-":
                    cur.pop(k2, None)
                elif v:
                    cur[k2] = v
            if cur:
                ag["provider"] = cur
            else:
                ag.pop("provider", None)
        # 工作引擎：type=api 或命令留空 → 清掉 engine 字段（回到接口引擎）
        if isinstance(body.get("engine"), dict):
            _eng_new = _engine_from_body(body["engine"], base=ag.get("engine") or {})
            if _eng_new:
                ag["engine"] = _eng_new
            else:
                ag.pop("engine", None)
        _save_config()
        _reload_agents()
        return JSONResponse({"ok": True})

    return JSONResponse({"error": "未知 action"}, status_code=400)


# ---------------------------------------------------------------------------
# 6d. 工作引擎：探测本机可用的 CLI（一键填好）+ 从身份文件一键建成员（分身配方）
# ---------------------------------------------------------------------------
ENGINE_PRESETS = [
    {"id": "claude", "title": "Claude Code", "bin": "claude",
     "cmd": 'claude -p "{prompt}"'},
    {"id": "codex", "title": "Codex CLI", "bin": "codex",
     "cmd": 'codex exec "{prompt}"'},
    {"id": "gemini", "title": "Gemini CLI", "bin": "gemini",
     "cmd": 'gemini -p "{prompt}"'},
    {"id": "aider", "title": "Aider", "bin": "aider",
     "cmd": 'aider --message "{prompt}" --yes'},
    {"id": "ollama", "title": "Ollama（本地模型，走 stdin）", "bin": "ollama",
     "cmd": 'ollama run llama3.2'},
]


@app.get("/api/agents/detect")
def detect_engines():
    """探测本机装了哪些能当引擎的 CLI，外加 DSH（照着 config.json 的 harness 段）。"""
    found = []
    if os.path.isfile(HARNESS_BIN):
        found.append({"id": "dsh", "title": "DeepSeek Harness（dsh，本机已有）",
                      "cmd": _engine_cmd_str([HARNESS_NODE, HARNESS_BIN,
                                              "--profile", "headless", "{prompt}"]),
                      "cwd": HARNESS_CWD, "timeout": 300,
                      "env": {"DSH_HOME": HARNESS_HOME}})
    for p in ENGINE_PRESETS:
        w = shutil.which(p["bin"])
        if w:
            found.append({"id": p["id"], "title": p["title"],
                          "cmd": p["cmd"], "bin_path": w})
    py = sys.executable or "python"
    found.append({"id": "python-demo", "title": "Python 脚本（示例，走 stdin）",
                  "cmd": _engine_cmd_str([py, "-c",
                                          "import sys;print(sys.stdin.read().upper())"]),
                  "bin_path": py})
    return JSONResponse({"found": found})


def _guess_name_from(files):
    """从身份文件里猜成员名字：先找 Name: 这类行，再退化到第一个标题，最后用文件名。"""
    for f in files:
        txt = str(f.get("content") or "")
        for pat in (r"(?im)^[-*\s]*\*{0,2}Name\*{0,2}\s*[:：]\s*\*{0,2}\s*([^\n*]+)",
                    r"(?im)^[-*\s]*名字\s*[:：]\s*([^\n]+)",
                    r"(?im)^#\s*([^\n#]+)"):
            m = re.search(pat, txt)
            if m:
                nm = m.group(1).strip().strip("*_` ")
                if nm and len(nm) <= 16:
                    return nm
    for f in files:
        base = os.path.splitext(os.path.basename(str(f.get("name") or "")))[0]
        if base:
            return base[:16]
    return "分身"


@app.post("/api/agents/import-personas")
async def import_personas(request: Request):
    """分身配方一键版：把身份文件存进工作台 personas/，并据此建一个新成员。
    body: {"files": [{"name": "SOUL.md", "content": "..."}], "name": "", "emoji": ""}
    """
    body = await request.json()
    files = [f for f in (body.get("files") or []) if isinstance(f, dict)]
    if not files:
        return JSONResponse({"error": "没有收到身份文件"}, status_code=400)
    name = (body.get("name") or "").strip() or _guess_name_from(files)
    key = re.sub(r"[^a-zA-Z0-9_-]", "", str(body.get("key") or "").strip())
    if not key:
        key = "ai" + str(int(time.time() * 1000) % 100000)
    if key in AI_CONF:
        return JSONResponse({"error": "标识 %s 已存在，换个 key" % key}, status_code=400)
    d = os.path.join(BASE_DIR, "personas", key)
    os.makedirs(d, exist_ok=True)
    saved = []
    for f in files:
        fn = os.path.basename(str(f.get("name") or "persona.md")) or "persona.md"
        if not fn.lower().endswith((".md", ".txt")):
            fn += ".md"
        fp = os.path.join(d, fn)
        with open(fp, "w", encoding="utf-8") as fh:
            fh.write(str(f.get("content") or ""))
        saved.append(fp)
    ag = {
        "key": key, "name": name,
        "emoji": (body.get("emoji") or "🤖").strip() or "🤖",
        "color": DEFAULT_COLORS[len(AI_CONF) % len(DEFAULT_COLORS)],
        "prefix": (key[:2].upper() + "_"),
        "workspace": os.path.join(BASE_DIR, "ws_" + key),
        "persona_files": saved,
        "model": "deepseek-flash",
        "trusted": False,
    }
    CONFIG.setdefault("agents", []).append(ag)
    _save_config()
    _reload_agents()
    return JSONResponse({"ok": True, "key": key, "name": name,
                         "files": saved, "dir": d})


def _read_dsh_conf():
    with open(DSH_WEB_PROFILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_dsh_conf(conf):
    """写回 dsh 配置；写前先备份，避免改坏导致引擎起不来"""
    try:
        with open(DSH_WEB_PROFILE, "r", encoding="utf-8") as f:
            old = f.read()
        with open(DSH_WEB_PROFILE + ".bak_twinai", "w", encoding="utf-8") as f:
            f.write(old)
    except Exception:
        pass
    with open(DSH_WEB_PROFILE, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)

def _plugin_meta(path):
    """从插件目录的 package.json 读元信息"""
    if not path:
        return {"version": "", "description": ""}
    pj = os.path.join(path, "package.json")
    if os.path.isfile(pj):
        try:
            with open(pj, "r", encoding="utf-8") as f:
                d = json.load(f)
            return {"version": d.get("version", ""), "description": d.get("description", "")}
        except Exception:
            pass
    return {"version": "", "description": ""}

@app.get("/api/engine/plugins")
def list_engine_plugins():
    """列出外部引擎(dsh web profile)的插件：已装(含启用状态) + 本地可安装"""
    try:
        conf = _read_dsh_conf()
    except Exception as e:
        return JSONResponse({"error": f"读取 dsh 配置失败：{e}"}, status_code=500)
    deps = conf.get("dependencies", {}) or {}
    bundles = (conf.get("dsh", {}).get("profile", {}) or {}).get("bundles", []) or []
    items = []
    for name, spec in deps.items():
        if name in DSH_BUILTIN_BUNDLES:
            continue
        path = spec[5:] if isinstance(spec, str) and spec.startswith("link:") else ""
        meta = _plugin_meta(path)
        items.append({"name": name, "installed": True, "enabled": name in bundles,
                      "path": path, "version": meta["version"],
                      "description": meta["description"]})
    # 扫描本地插件目录里尚未安装的
    try:
        for d in sorted(os.listdir(ENGINE_PLUGINS_DIR)):
            fp = os.path.join(ENGINE_PLUGINS_DIR, d)
            pj = os.path.join(fp, "package.json")
            if os.path.isdir(fp) and os.path.isfile(pj):
                try:
                    with open(pj, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    continue
                nm = meta.get("name", d)
                if nm not in deps:
                    items.append({"name": nm, "installed": False, "enabled": False,
                                  "path": fp, "version": meta.get("version", ""),
                                  "description": meta.get("description", "")})
    except Exception:
        pass
    return JSONResponse({"plugins": items, "profile": DSH_WEB_PROFILE,
                         "dir": ENGINE_PLUGINS_DIR})

@app.post("/api/engine/plugins/toggle")
async def toggle_engine_plugin(request: Request):
    """启用/停用插件（改 bundles 数组，dsh 为 live 重载，无需重启）"""
    body = await request.json()
    name = (body.get("name") or "").strip()
    enable = bool(body.get("enabled"))
    if not name:
        return JSONResponse({"error": "缺少插件名"}, status_code=400)
    conf = _read_dsh_conf()
    dsh = conf.setdefault("dsh", {}).setdefault("profile", {})
    bundles = dsh.setdefault("bundles", [])
    if enable and name not in bundles:
        bundles.append(name)
    elif not enable and name in bundles:
        bundles.remove(name)
    _write_dsh_conf(conf)
    return JSONResponse({"ok": True, "name": name, "enabled": name in bundles})

@app.post("/api/engine/plugins/install")
async def install_engine_plugin(request: Request):
    """安装本地插件目录里的插件（link: 方式加 dependency + 加入 bundles）"""
    body = await request.json()
    path = (body.get("path") or "").strip()
    if not path or not os.path.isdir(path):
        return JSONResponse({"error": "插件目录不存在"}, status_code=400)
    pj = os.path.join(path, "package.json")
    if not os.path.isfile(pj):
        return JSONResponse({"error": "该目录没有 package.json，不是合法插件"}, status_code=400)
    with open(pj, "r", encoding="utf-8") as f:
        meta = json.load(f)
    name = meta.get("name") or os.path.basename(path)
    conf = _read_dsh_conf()
    conf.setdefault("dependencies", {})[name] = f"link:{path}"
    dsh = conf.setdefault("dsh", {}).setdefault("profile", {})
    bundles = dsh.setdefault("bundles", [])
    if name not in bundles:
        bundles.append(name)
    _write_dsh_conf(conf)
    return JSONResponse({"ok": True, "name": name, "enabled": True,
                         "version": meta.get("version", "")})

@app.post("/api/engine/plugins/uninstall")
async def uninstall_engine_plugin(request: Request):
    """卸载插件（移除 dependency + 从 bundles 移除）"""
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "缺少插件名"}, status_code=400)
    conf = _read_dsh_conf()
    conf.get("dependencies", {}).pop(name, None)
    bundles = conf.get("dsh", {}).get("profile", {}).get("bundles", [])
    if name in bundles:
        bundles.remove(name)
    _write_dsh_conf(conf)
    return JSONResponse({"ok": True, "name": name})

def stream_single(ai_key, sid, message, attachments=None):
    # 插件钩子：before_message 可改写消息
    hp = run_hooks("before_message", {"session_id": sid, "mode": "single",
                                      "ai": ai_key, "message": message,
                                      "agents": [ai_key]})
    message = hp.get("message", message)
    data = load_session(sid)
    ai = AI_CONF[ai_key]
    # 记录用户消息（附件只存元信息，内容本身不落会话，避免 session 被图片撑爆）
    atts = _att_meta(attachments)
    data.setdefault(ai_key, []).append({"role": "user", "content": message})
    if atts:
        data[ai_key][-1]["attachments"] = atts
    log_and_save(sid, data, f"用户→{ai['name']}", message)
    # 先告诉前端"谁要说话"，再实时推流
    yield sse({"type": "who", "ai": ai_key, "name": ai["name"], "emoji": ai.get("emoji","")})

    # 插件钩子：command —— /开头的消息可由插件直接作答
    handled = handle_command(sid, "single", ai_key, message)
    if handled is not None:
        handled = _wrap_before_send(sid, "single", ai_key, handled)
        for chunk in chunk_text(handled):
            yield sse({"type": "token", "ai": ai_key, "text": chunk})
        data[ai_key].append({"role": "assistant", "content": handled})
        log_and_save(sid, data, ai["name"], handled)
        save_session(sid, data)
        run_hooks("after_reply", {"session_id": sid, "mode": "single",
                                  "ai": ai_key, "text": handled})
        yield sse({"type": "done", "ai": ai_key})
        return

    parts = []
    _think_buf = []      # 思考过程：落盘，刷新后还能展开看
    _tools_buf = []      # 工具调用卡片：同上
    _files_buf = []      # 这次写出来的文件：登记进产出表
    for ev in run_agent_stream(ai_key, _mm_prepare(data[ai_key])):
        if ev["type"] == "think":
            _think_buf.append(ev["text"])
            yield sse({"type": "think", "ai": ai_key, "text": ev["text"]})
        elif ev["type"] == "token":
            parts.append(ev["text"])
            yield sse({"type": "token", "ai": ai_key, "text": ev["text"]})
        elif ev["type"] == "tool":
            _tools_buf.append({k: v for k, v in ev.items() if k != "type"})
            for _f in (ev.get("files") or []):
                if _f and _f.get("path"):
                    _files_buf.append(_f)
            yield sse({"type": "tool", "ai": ai_key,
                       **{k: v for k, v in ev.items() if k != "type"}})
        elif ev["type"] == "usage":
            yield sse({"type": "usage", "ai": ai_key,
                       **{k: v for k, v in ev.items() if k != "type"}})
    raw = "".join(parts)
    # 插件钩子：before_send —— 最终文案加工；改了会推 replace 覆盖已流出的气泡
    final = _wrap_before_send(sid, "single", ai_key, raw)
    data[ai_key].append(_session_reply(final, _think_buf, _tools_buf, _files_buf))
    _remember_session_files(sid, _files_buf)
    log_and_save(sid, data, ai["name"], final)
    save_session(sid, data)
    if final != raw:
        yield sse({"type": "replace", "ai": ai_key, "text": final})
    run_hooks("after_reply", {"session_id": sid, "mode": "single",
                              "ai": ai_key, "text": final})
    yield sse({"type": "done", "ai": ai_key})

@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    ai_key = body.get("ai", "agent_a")
    if ai_key not in AI_CONF:
        return JSONResponse({"error": f"未知 AI：{ai_key}"}, status_code=400)
    sid = body.get("session_id", "default")
    message = body.get("message", "").strip()
    attachments = body.get("attachments") or []
    # 允许「只发附件不说话」
    if not message and not attachments:
        return JSONResponse({"error": "empty message"}, status_code=400)
    return StreamingResponse(_safe_stream(stream_single(ai_key, sid, message, attachments), "single"),
                             media_type="text/event-stream")

# ---------------------------------------------------------------------------
# 7b. 群聊模式：用户 + 多个 AI 在同一条消息流里混着说话
#     · 被点名（消息里含成员名）→ 必回
#     · 都没点名 → 各自判断"该不该我接话"，避免两个 AI 每句都抢着回
#     · 用户可随时插话（前端会中断上一轮再发新的）
# ---------------------------------------------------------------------------
GROUP_EXTRA = """
【群聊模式】你现在在一个多人群聊里：用户「你」、还有其他 AI：{roster}。
{role_line}
- 你**不是每条消息都必须回**。只在确实该你接话时才说：被点名、有新问题、你能补充关键信息、对方说得不对、或话题正好归你负责。
- 别人已经答到位、只是闲聊收尾、或与你无关时，就别硬接话（不发"收到""好的"这类水词）。
- 说话要短，像在群里打字，不要长篇大论、不要复述对方说过的话。
- 你可以直接点名其他 AI（{names}），它看得到。
- 如果用户的原始要求里规定了「做完某事后要继续下一步」（例如「编辑完后自动总结」「写完让他检查」）：干完活的人必须在回复结尾**明确点名**负责下一步的成员并说清要做什么（如「@DS 请总结」），不要以为对方会自己看到——点名后系统会自动让对方接话；如果下一步还是你自己做（比如还要写下一个文件），就在结尾直接 @自己的名字，系统会让你继续。
"""

# 组长 / 平权 / 旁听：每个成员在群聊里的身份（settings.groupRoles 存 {key: role}）
ROLE_LINE = {
    "lead": "- 你是**本群组长**：用户的需求由你接、由你拆解，需要干活时直接点名派工给其他 AI，最后由你汇总结果。",
    "peer": "- 你与大家平权：谁该说话谁说，不用等谁指派。",
    "audience": "- 你是**旁听**：除非被点名，否则不要主动接话，安静看大家讨论。",
}

GROUP_WHO = {"user": "你"}

def _who(role):
    """群聊历史里 role → 显示名"""
    if role in GROUP_WHO:
        return GROUP_WHO[role]
    if role in AI_CONF:
        return AI_CONF[role]["name"]
    return str(role)

def _arbiter_prov(cands, history):
    """判定类小通话（谁该发言/还有没有活儿）用哪个成员的接口？
    优先「最近发言的成员」——它刚刚还成功说过话，接口必然可用；
    其次候选里第一个接口就绪的。绝不依赖全局默认配置：它可能是坏的
    （401 会把判定吞成静默失败，群聊自动接力/该谁发言就全废了）。"""
    for h in reversed(history[-6:]):
        k = h.get("role")
        if k in AI_CONF and _prov_ready(k):
            return _prov(k)
    for k in cands:
        if k in AI_CONF and _prov_ready(k):
            return _prov(k)
    return None


def _decide_speak(history, agents):
    """
    一次 API 同时判定 agents 里谁该接话，返回 {key: bool}。
    判定失败 → 全 True（宁可多说，别冷场）。只给被点到的名字行解析，长名字先匹配。
    """
    names = {k: AI_CONF[k]["name"] for k in agents if k in AI_CONF}
    lines = []
    for h in history[-10:]:
        lines.append(f"{_who(h.get('role'))}：{(h.get('content') or '')[:200]}")
    roster = "、".join(names.values())
    p = ("下面是群聊（参与者：用户「你」、" + roster + "）最近的对话：\n"
         + "\n".join(lines)
         + "\n\n最后这条消息，以下 AI 分别需要回应吗？\n"
         + "\n".join(names.values())
         + "\n判断标准：被点名→需要；别人已答到位/纯闲聊收尾/与你无关→不需要；"
           "能补充关键信息或纠正对方错误→需要。\n"
           f"严格只输出 {len(names)} 行，每行格式「名字=yes/no」，不要解释。")
    try:
        # 判定是小活儿：关思考模式（新模型默认开思考，会拖慢群聊首字、还烧钱）；
        # max_tokens 60，避免模型多输出几个字就被截断、解析成 no 引发抢答。
        # 走「最近发言成员」的接口而不是全局默认——默认配置可能是坏的（401 静默失败，
        # 以前就是这里一直炸，导致「该谁发言」判定从未生效、回退成全员都说话）
        prov = _arbiter_prov(list(names), history)
        if prov is None:
            return {k: True for k in names}
        r = _api_call([{"role": "user", "content": p}], None, max_tokens=60,
                      extra={"thinking": {"type": "disabled"}}, prov=prov)
        ans = (r["choices"][0]["message"].get("content") or "").strip().lower()
        res = {k: None for k in names}
        # 长名字先匹配，避免「小助手」吃掉「助手」这类包含关系
        for k in sorted(names, key=lambda x: -len(names[x])):
            nm = names[k].lower()
            for line in ans.replace("：", "=").splitlines():
                if nm in line:
                    res[k] = "yes" in line
                    break
        if all(v is None for v in res.values()):
            return {k: True for k in names}
        return {k: bool(res[k]) for k in names}
    except Exception:
        return {k: True for k in names}


def _relay_arbiter(user_msg, history, agents, spoke):
    """
    判定式自动接力：用户最初的要求里若规定了「做完某事后继续下一步」
    （如「编辑完后自动总结」「写完让他检查」），干完活却没人接着做时，
    这里负责把下一步的人叫起来。关键词点名（_detect_relay_targets）覆盖不到
    这类场景——任务分配写在用户消息里、不在 AI 的回复里。
    返回应接话的 agent key；None = 任务已闭环，不用接力。
    注意：候选是全体在场成员（可以已经发过言）——「干完一步还要干下一步」往往
    就是同一个人，若只允许叫没发言过的人，会出现「把监控位的叫起来、干活的
    反而永远续不上」的冷场。
    """
    cands = [k for k in agents if k in AI_CONF]
    if not cands:
        return None
    names = {k: AI_CONF[k]["name"] for k in cands}
    lines = [f"{_who(h.get('role'))}：{(h.get('content') or '')[:200]}"
             for h in history[-12:]]
    p = ("群聊任务进度盘点。用户最初的要求：" + (user_msg or "")[:300]
         + "\n最近对话：\n" + "\n".join(lines)
         + "\n\n对照用户最初的要求：是否还有「安排过、但还没人做」的后续步骤"
           "（例如「编辑完后自动总结」里的总结、「写完让他检查」里的检查、"
           "有人明说要写的下一个文件却还没写）？"
           "若有，只输出最该做这件事的成员名字（可以已经发过言，比如干活的那个"
           "要继续做下一步）；若全部已完成或纯属闲聊收尾，只输出「无」。不要解释。")
    prov = _arbiter_prov(cands, history)
    if prov is None:
        return None
    try:
        # 和 _decide_speak 同款：小活儿，关思考、限输出，慢了拖群聊、贵了烧钱。
        # 走「最近发言成员」的接口而不是全局默认——默认配置可能是坏的（401 静默失败）
        r = _api_call([{"role": "user", "content": p}], None, max_tokens=30,
                      extra={"thinking": {"type": "disabled"}}, prov=prov)
        ans = (r["choices"][0]["message"].get("content") or "").strip()
        if not ans or ans[:6].startswith("无"):
            return None
        for k in sorted(names, key=lambda x: -len(names[x])):
            if names[k] and names[k] in ans:
                return k
    except Exception:
        return None
    return None


def _detect_relay_targets(text, agents, exclude_keys):
    """
    检测某位 AI 的回复里是否「点名派工」给其他在场的 AI（群聊自动接力用）：
      · 显式 @名字（不排除已发言者：干活的人 @自己 = 还要继续做下一步；
        别人 @他 = 明确把活交回给他。总轮数由 relay_max 硬顶，不会套娃）
      · 或「名字，/名字：」这类被称呼，且附近有派工意图（你/请/帮我/交给你/拜托你…）
        —— 模糊称呼仍排除已说过的人，避免客套话引发重复发言。
    """
    text_l = text or ""
    targets = set()
    relay_marks = ("你来", "请", "帮我", "交给你", "拜托你", "你查", "你看看",
                   "你接手", "你负责", "你分析", "你验证", "你确认", "你回答",
                   "你补充", "你处理", "你搞定", "你接着")
    for k in agents:
        nm = AI_CONF.get(k, {}).get("name", "")
        if not nm or nm not in text_l:
            continue
        if "@" + nm in text_l:          # 显式 @点名 → 直接接力（已发言者也算）
            targets.add(k)
            continue
        if k in exclude_keys:
            continue
        # 名字被「称呼」（后跟标点），且这一小段里含派工意图
        idx = 0
        while True:
            i = text_l.find(nm, idx)
            if i < 0:
                break
            idx = i + len(nm)
            after = text_l[i + len(nm): i + len(nm) + 28]
            if after[:1] in ("，", "：", ":", "、", "。", " ", ","):
                if any(m in after for m in relay_marks):
                    targets.add(k)
                    break
    return targets


def group_speakers(message, history, agents=None, roles=None):
    """
    本轮谁发言（agents 为本场群聊成员，roles 为 {key: lead|peer|audience}）：
      · 被点名（消息含某成员名字）→ 该 AI 必说（不看身份与开关）
      · 都没点名：
          groupDecide=False → 全员都回（首字更快，可能重复）
          有组长           → 组长必到（接需求/派工），其余非旁听成员由判定决定是否补充
          无组长           → 非旁听成员全部交判定，该说才说
      · 一个人都不说 → 组长兜底；没组长就第一个非旁听成员兜底，避免冷场
    """
    agents = [k for k in (agents or AGENT_ORDER) if k in AI_CONF]
    roles = roles or {}
    m = message or ""
    forced = [k for k in agents if AI_CONF[k]["name"] in m]
    if forced:
        return forced
    if len(agents) <= 1:
        return agents
    if not SETTINGS.get("groupDecide", True):
        return agents
    lead = [k for k in agents if roles.get(k) == "lead"]
    judge = [k for k in agents
             if k not in lead and roles.get(k) != "audience"]
    out = list(lead)  # 组长接需求，必到
    if judge:
        res = _decide_speak(history, judge)
        out += [k for k in judge if res.get(k)]
    if out:
        return out
    # 兜底：组长优先，否则第一个非旁听成员
    if lead:
        return lead
    non_aud = [k for k in agents if roles.get(k) != "audience"]
    return non_aud[:1] or agents[:1]


def build_group_conv(history, ai_key):
    """把群聊历史映射成该 AI 视角的 user/assistant 对话（任意成员名）"""
    conv = []
    for h in history:
        r, c = h.get("role"), (h.get("content") or "")
        if r == "user":
            conv.append(_mm_one(h) if h.get("attachments")
                        else {"role": "user", "content": c})
        elif r == ai_key:
            conv.append({"role": "assistant", "content": c})
        elif r in AI_CONF:
            conv.append({"role": "user", "content": f"【{AI_CONF[r]['name']} 说】：{c}"})
        # 未知角色（该 AI 已被删除）→ 跳过，避免 KeyError
    return conv

def _safe_stream(inner, label=""):
    """总兜底：任何异常都不允许把 SSE 流「无声掐死」。

    以前生成器里任何一处没兜住的异常（特殊文件、插件钩子、序列化…）都会让
    整条流中途断掉 —— 前端永远停在「正在写…」，用户看到的就是假死/死进程。
    现在统一收口：转成一条可见的错误消息 + 正常收尾信号，前端一定能落地。
    """
    try:
        for ev in inner:
            yield ev
    except GeneratorExit:
        raise          # 客户端主动断开属正常行为，原样上抛
    except Exception as e:
        try:
            yield sse({"type": "token",
                       "text": f"\n\n⚠️ [服务内部错误，本条回复被截停：{type(e).__name__}: {e}]"})
            yield sse({"type": "all-done"})
        except Exception:
            pass
    finally:
        try:
            inner.close()
        except Exception:
            pass


@app.on_event("startup")
def _bump_threadpool():
    """同步端点线程池从默认 40 提到 120：个别操作卡住也耗不光线程池，服务不再被拖成假死。"""
    try:
        from anyio import to_thread as _tt
        _tt.current_default_thread_limiter().total_tokens = 120
    except Exception:
        pass


@app.post("/api/group")
async def group_chat(request: Request):
    body = await request.json()
    sid = body.get("session_id", "default")
    message = (body.get("message") or "").strip()
    attachments = body.get("attachments") or []
    if not message and not attachments:
        return JSONResponse({"error": "empty message"}, status_code=400)

    def gen():
        # 本场群聊成员：前端可自选哪几个 AI 进场（默认全员）
        agents = [k for k in (body.get("agents") or AGENT_ORDER) if k in AI_CONF] or AGENT_ORDER
        roles = SETTINGS.get("groupRoles", {}) or {}
        # 插件钩子：before_message 可改写消息或调整在场成员
        # 注意：插件改后的消息存到 msg_now，别写回外层闭包变量 message（会变局部变量报错）
        hp = run_hooks("before_message", {"session_id": sid, "mode": "group",
                                          "message": message, "agents": agents,
                                          "all_speak": bool(body.get("all_speak"))})
        msg_now = hp.get("message", message)
        agents = [k for k in (hp.get("agents") or agents) if k in AI_CONF] or agents
        data = load_session(sid)
        g = data.setdefault("group", [])
        g.append({"role": "user", "content": msg_now})
        if _att_meta(attachments):
            g[-1]["attachments"] = _att_meta(attachments)
        log_and_save(sid, data, "你", msg_now)
        # 插件钩子：command —— /开头的消息可由插件直接作答（只回一条，指名第一个在场成员）
        if agents:
            cmd_reply = handle_command(sid, "group", agents[0], msg_now,
                                       {"agents": agents})
            if cmd_reply is not None:
                who = agents[0]
                ai = AI_CONF[who]
                cmd_reply = _wrap_before_send(sid, "group", who, cmd_reply)
                yield sse({"type": "who", "ai": who, "name": ai["name"],
                           "emoji": ai.get("emoji", "")})
                for chunk in chunk_text(cmd_reply):
                    yield sse({"type": "token", "ai": who, "text": chunk})
                g.append({"role": who, "content": cmd_reply})
                data["group"] = g
                log_and_save(sid, data, ai["name"], cmd_reply)
                save_session(sid, data)
                run_hooks("after_reply", {"session_id": sid, "mode": "group",
                                          "ai": who, "text": cmd_reply})
                yield sse({"type": "done", "ai": who})
                yield sse({"type": "all-done"})
                return
        # 决定谁接话（history 已含刚发的这条）
        # all_speak=True：全员必答（原「同时问」并入群聊，一条流里全员依次回）
        speakers = list(agents if body.get("all_speak") else group_speakers(message, g, agents, roles))

        def speak_one(ai_key, relay_round=0):
            """让单个成员发言（群聊主轮或自动接力轮通用）。yield SSE 事件，return 最终文本。"""
            ai = AI_CONF[ai_key]
            roster = "、".join(f"「{AI_CONF[k]['name']}」" for k in agents if k != ai_key)
            sysp = (SYSTEM_PROMPTS[ai_key] + "\n"
                    + GROUP_EXTRA.format(
                        roster=roster or "（暂时没有其他 AI，你先直接回应用户）",
                        role_line=ROLE_LINE.get(roles.get(ai_key, "peer"), ROLE_LINE["peer"]),
                        names=roster or "（无）"))
            conv = build_group_conv(g, ai_key)
            if relay_round > 0:
                yield sse({"type": "relay", "ai": ai_key, "round": relay_round})
            yield sse({"type": "who", "ai": ai_key, "name": ai["name"],
                       "emoji": ai.get("emoji", "")})
            parts = []
            _think_buf = []
            _tools_buf = []
            _files_buf = []
            for ev in run_agent_stream(ai_key, conv, True, sysp):
                if ev["type"] == "think":
                    _think_buf.append(ev["text"])
                    yield sse({"type": "think", "ai": ai_key, "text": ev["text"]})
                elif ev["type"] == "token":
                    parts.append(ev["text"])
                    yield sse({"type": "token", "ai": ai_key, "text": ev["text"]})
                elif ev["type"] == "tool":
                    _tools_buf.append({k: v for k, v in ev.items() if k != "type"})
                    for _f in (ev.get("files") or []):
                        if _f and _f.get("path"):
                            _files_buf.append(_f)
                    yield sse({"type": "tool", "ai": ai_key,
                               **{k: v for k, v in ev.items() if k != "type"}})
                elif ev["type"] == "usage":
                    yield sse({"type": "usage", "ai": ai_key,
                               **{k: v for k, v in ev.items() if k != "type"}})
            raw = "".join(parts)
            # 插件钩子：before_send —— 最终文案加工（改了推 replace 覆盖气泡）
            final = _wrap_before_send(sid, "group", ai_key, raw)
            _rec = _session_reply(final, _think_buf, _tools_buf, _files_buf)
            _rec["role"] = ai_key
            g.append(_rec)
            _remember_session_files(sid, _files_buf)
            data["group"] = g
            log_and_save(sid, data, ai["name"], final)
            save_session(sid, data)
            if final != raw:
                yield sse({"type": "replace", "ai": ai_key, "text": final})
            run_hooks("after_reply", {"session_id": sid, "mode": "group",
                                      "ai": ai_key, "text": final})
            yield sse({"type": "done", "ai": ai_key})
            return final

        # 主轮
        spoke = []
        relay_pending = set()
        for ai_key in speakers:
            final = yield from speak_one(ai_key)
            spoke.append(ai_key)
            # 收集本轮被点名派工的人作为接力候选（函数内部：显式@不排除已发言，
            # 模糊称呼仍排除已发言）
            relay_pending |= _detect_relay_targets(final, agents, set(spoke))

        # ④ 自动接力：被点名派工的 AI 自动接话，最多 relay_max 轮，避免无限套娃
        try:
            relay_max = max(0, int(SETTINGS.get("relayMaxRounds", 2)))
        except Exception:
            relay_max = 2
        rnd = 0
        while rnd < relay_max:
            # 候选优先级：AI 回复里显式点名的（可含已发言者/自己，支持连续干几步）
            # > 判定式发现的「用户安排了但没人做的」
            this = sorted(relay_pending)
            relay_pending = set()
            if not this:
                nxt = _relay_arbiter(msg_now, g, agents, set(spoke))
                if not nxt:
                    break
                this = [nxt]
            rnd += 1
            for ai_key in this:
                final = yield from speak_one(ai_key, rnd)
                spoke.append(ai_key)
                relay_pending |= _detect_relay_targets(final, agents, set(spoke))
        yield sse({"type": "all-done"})
    return StreamingResponse(_safe_stream(gen(), "group"), media_type="text/event-stream")

# ---------------------------------------------------------------------------
# 8. 工作台插件系统（不改核心代码，就能改工作台的「一切」）
#    目录：plugins/<插件名>/
#      plugin.json   清单（必须）：name/title/version/author/description/main/panel
#                    + ui 段（可选，直接改界面）：
#                       css / js / cssText / jsText / vars / bodyClass /
#                       snippets(插槽注入) / background(背景板) / title / favicon
#      main.py       后端扩展（可选）：def register(api): ...
#      ui.css/ui.js  界面改造（可选）：背景板、配色、圆角、行为
#      panel.html    面板页（可选）：设置里点「打开面板」直接看到
#      <任意素材>    图片/字体/视频等，通过 /ext/<插件名>/xxx 访问
#    11 个钩子：app_start / settings / agents / ui_assets / system_prompt /
#              before_message / command / before_request / on_tool /
#              after_reply / before_send
#    前端侧 window.WB：设置/成员/toast/加功能栏图标/加设置节/事件总线/弹面板/发消息
#    改完在设置里点「重新扫描」+「⟳ 重载界面」即可热加载，不用重启服务
# ---------------------------------------------------------------------------
PLUGIN_DIR = os.path.join(BASE_DIR, "plugins")

# —— 可选扩展点：自己写一段接管逻辑（默认关闭）——
# 若本目录下存在 extensions/ 且里面有 router.py（需实现
# is_program_code(path) 与 route(path, content, who, prefix)），则载入它，
# 用来接管「AI 改本程序代码」这类请求（例如转交给外部 IDE / 另一个 AI）。
# 默认没有这个目录，整段恒为 None，无任何行为。
_EXT_ROUTER = None
_pp_dir = os.path.join(BASE_DIR, "extensions")
if os.path.isdir(_pp_dir):
    try:
        if _pp_dir not in sys.path:
            sys.path.insert(0, _pp_dir)
        import router as _ext_router_mod
        _EXT_ROUTER = _ext_router_mod
    except Exception as _e:
        print(f"[可选扩展] extensions/ 载入失败（已忽略）：{type(_e).__name__}: {_e}")

PLUGIN_STATE = os.path.join(BASE_DIR, ".plugins_state.json")
PLUGINS = {}        # 插件名 -> 清单
HOOKS = {}          # 钩子名 -> [(优先级, 插件名, 函数)]
PLUGIN_ERRORS = {}  # 插件名 -> 载入错误
PLUGIN_ROUTES = []  # 插件注册过的路由 [(插件名, path)]，热重载时先摘掉再挂
SUBSCRIBERS = []    # 前端实时订阅队列（插件 api.emit 推的事件从这里下发）
SUB_LOCK = threading.Lock()

# 插件可用的全部钩子（同时也是开发指南里的清单）
HOOK_NAMES = [
    "app_start",       # 插件全部载入完成（初始化自己的状态）
    "settings",        # 前端读设置：可改写设置（锁主题、强制字号…）
    "agents",          # 前端读成员：可改名/换色/隐藏/加字段
    "ui_assets",       # 前端拉界面资源：注入 CSS/JS/HTML/变量/背景板
    "system_prompt",   # 构造系统提示：改人格、加规则、加人设
    "before_message",  # 用户消息进来：改消息 / 换在场成员 / 换模型
    "command",         # 以 / 开头的消息：插件自定义指令，可直接作答
    "before_request",  # 每次请求模型前：换模型、换平台、改参数、改 messages
    "on_tool",         # 工具执行完：可改结果、拦截、记账
    "after_reply",     # 某个 AI 说完：改落库文本、记录、统计
    "before_send",     # 回复推到前端前：最终文案加工（翻译/加签名/排版）
]

# 前端可被插件注入内容的插槽（对应 index.html 里的容器）
UI_SLOTS = ["head", "body", "top", "bottom", "rail", "clist", "chat",
            "chatbar", "settings", "left", "right"]

PLUGIN_GUIDE = """【工作台插件 —— 可以改工作台的一切】
一、目录：plugins/<插件名>/，必须有 plugin.json，其余文件都可有可无。
   plugin.json / main.py（后端钩子）/ panel.html（设置里的面板）/ ui.css / ui.js / 图片素材…

二、plugin.json 示例（ui 段 = 直接改界面，是最常用来"换皮"的地方）：
   {
     "name": "my-skin",
     "title": "我的皮肤",
     "version": "1.0.0",
     "author": "你",
     "description": "换背景板 + 改圆角 + 加个按钮",
     "main": "main.py",
     "panel": "panel.html",
     "ui": {
       "css": ["ui.css"],
       "js":  ["ui.js"],
       "snippets": {"rail": ["rail.html"], "settings": ["set.html"]},
       "vars": {"--accent": "#f0b429", "--panel": "rgba(255,255,255,.72)"},
       "bodyClass": ["my-skin"],
       "background": {"image": "bg.svg", "size": "cover", "opacity": 1},
       "title": "我的工作台",
       "favicon": "icon.svg"
     }
   }
   · css/js 里写相对路径即可（自动映射成 /ext/<插件名>/xxx）
   · vars 直接覆盖前端 CSS 变量：--bg --panel --panel2 --line --text --muted --accent
     --user-bg --r-msg --fs --lh --mw …（想看清背景板就把面板改成半透明）
   · snippets 把 HTML 片段塞进指定插槽：head/body/top/bottom/rail/clist/chat/chatbar/settings
   · background 支持 image/size/position/repeat/attachment/opacity/blur/color

三、main.py —— 后端扩展：
   def register(api):
       @api.hook("system_prompt")            # 改所有 AI 的人格/规则
       def sp(p):
           p["prompt"] += "\\n【口头禅】说话结尾带一个✨"
           return p
       @api.hook("command")                  # 自定义指令：/天气 xxx
       def cmd(p):
           if p["cmd"] == "天气":
               return {"handled": True, "reply": "今天晴，适合开工。"}
       @api.hook("before_send")              # 回复最终加工（翻译/加签名/排版）
       def bs(p):
           p["text"] = "【" + p["ai"] + "】" + p["text"]
           return p
       @api.route("GET", "/api/my-skin/ping")   # 自己加接口
       def ping():
           return {"ok": True, "count": api.store.get("count", 0)}

四、可用钩子（按调用顺序）：
   app_start       {}                                  插件载入完成
   settings        {settings}                          改设置
   agents          {agents}                            改成员列表
   ui_assets       {items}                             改界面注入（可动态生成 CSS/JS）
   system_prompt   {ai, prompt}                        改系统提示
   before_message  {session_id, mode, message, agents} 改消息/成员
   command         {session_id, mode, ai, cmd, args}   /指令，返回 {"handled":True,"reply":"..."}
   before_request  {ai, model, provider, messages}     换模型/平台/参数
   on_tool         {ai, name, args, ok, result}        改工具结果
   after_reply     {session_id, mode, ai, text}        改落库文本
   before_send     {ai, text}                          改最终展示文案
   · 钩子按 priority 从小到大执行（默认 50）；返回 dict 即替换 payload。
   · 抛异常不会搞挂服务，会打印在服务端日志里。

五、api 提供的能力：
   api.hook(名, priority=50)      注册钩子
   api.route("GET", "/api/x")     注册接口
   api.store.get/set/all/update/delete   插件私有存储（plugins/<名>/.data.json，重启不丢）
   api.settings() / api.set_settings({...})   读写工作台设置
   api.agents()                   读全部成员
   api.emit("事件名", {...})      实时推给所有在线前端（前端 WB.on('plugin:事件名') 收到）
   api.path("a.png")             插件目录下的文件绝对路径
   api.url("a.png")              → /ext/<插件名>/a.png（可放进 <img src>）
   api.shared_path("x.md")       共享记忆目录下的文件路径
   api.log(...)                  打印到服务端日志

六、前端 JS 侧（ui.js 里能用的全局对象 window.WB）：
   WB.settings / WB.patchSettings({...})   读改设置（改了立刻生效并保存）
   WB.agents()                            成员列表
   WB.toast("文字")                        右下角提示
   WB.addRailButton({icon:"🎨", title:"换皮", onclick(){...}})   往左侧功能栏加图标
   WB.addSettingSection("标题", fn)         往设置面板加一节（fn 收到容器元素）
   WB.slot("rail")                        取插槽容器，自己往里塞元素
   WB.on("reply", e=>{})                  事件：user / reply / tool / who / done
   WB.on("plugin:事件名", e=>{})           收到后端 api.emit 推来的事件
   WB.emit("事件名", data)                 发给其他前端脚本
   WB.openPanel("标题", DOM元素)            弹一个自定义面板窗
   WB.send("文字")                         以当前会话替用户发一条消息
   WB.reloadUI()                          重新拉取插件界面资源（改完插件点它）

七、安全：插件在服务进程内跑，等同于本机代码；只装你自己信得过（或看得懂）的插件。
   停用：设置 → 工作台插件 → 停用（状态记在 .plugins_state.json）。"""


def _plugins_state():
    try:
        return json.loads(open(PLUGIN_STATE, encoding="utf-8").read())
    except Exception:
        return {}


def _save_plugins_state(st):
    with open(PLUGIN_STATE, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=2)


class PluginStore:
    """插件私有 KV 存储：plugins/<插件名>/.data.json（重启不丢）"""

    def __init__(self, path):
        self.path = path

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                d = json.loads(fh.read() or "{}")
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _write(self, d):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def all(self):
        return self._read()

    def get(self, key, default=None):
        return self._read().get(key, default)

    def set(self, key, val):
        d = self._read()
        d[key] = val
        self._write(d)
        return val

    def update(self, mapping):
        d = self._read()
        d.update(mapping or {})
        self._write(d)
        return d

    def delete(self, key):
        d = self._read()
        d.pop(key, None)
        self._write(d)

    def clear(self):
        self._write({})


def broadcast(event, data=None, plugin=""):
    """把事件实时推给所有在线前端（前端订阅 /api/plugins/stream）"""
    msg = {"type": "plugin-event", "event": event,
           "plugin": plugin, "data": data or {}}
    with SUB_LOCK:
        for q in list(SUBSCRIBERS):
            try:
                q.put_nowait(msg)
            except Exception:
                try:
                    SUBSCRIBERS.remove(q)
                except ValueError:
                    pass
    return True


class PluginAPI:
    """交给插件的接口对象（main.py 里 def register(api): ...）"""

    def __init__(self, name, plug_dir=None):
        self.name = name
        self.dir = plug_dir or os.path.join(PLUGIN_DIR, name)
        self.store = PluginStore(os.path.join(self.dir, ".data.json"))

    # ---------- 注册 ----------
    def hook(self, hook_name, fn=None, priority=50):
        def deco(f):
            HOOKS.setdefault(hook_name, []).append((int(priority), self.name, f))
            HOOKS[hook_name].sort(key=lambda x: x[0])
            return f
        return deco(fn) if fn else deco

    def route(self, method, path):
        def deco(f):
            # 防呆：不允许顶掉工作台已有接口（先摘掉自己上一轮注册的再判断）。
            # 注意要比「路径 + 方法」：同一路径的 GET 与 POST 是两条独立路由，
            # 只看路径会把插件自己刚注册的那个方法当成重复吞掉 ——
            # 表现为该接口 405 Method Not Allowed，而日志只说「已存在」，很难猜。
            m = method.upper()
            for r in app.router.routes:
                if getattr(r, "path", None) != path:
                    continue
                if m in (getattr(r, "methods", None) or set()):
                    self.log(f"路由 {m} {path} 已存在，跳过（避免覆盖工作台接口）")
                    return f
            app.add_api_route(path, f, methods=[m])
            PLUGIN_ROUTES.append((self.name, path))
            return f
        return deco

    def log(self, *a):
        print(f"[插件:{self.name}]", *a)

    # ---------- 路径 ----------
    def path(self, *parts):
        """插件自己目录下的文件绝对路径"""
        return os.path.join(self.dir, *parts)

    def url(self, rel):
        """插件静态资源地址：/ext/<插件名>/<rel>（可直接塞进 <img src>、CSS url()）"""
        return f"/ext/{self.name}/{str(rel).lstrip('/')}"

    def shared_path(self, *parts):
        return os.path.join(SHARED_MEMORY, *parts)

    # ---------- 工作台数据 ----------
    def settings(self):
        return dict(SETTINGS)

    def set_settings(self, vals):
        return save_settings(vals or {})

    def agents(self):
        return {k: dict(v) for k, v in AI_CONF.items()}

    # ---------- 实时推前端 ----------
    def emit(self, event, data=None):
        return broadcast(event, data, self.name)


def load_plugins():
    """扫描 plugins/ 并载入已启用的插件；可重复调用 = 热重载"""
    global PLUGINS, HOOKS, PLUGIN_ERRORS
    PLUGINS, HOOKS, PLUGIN_ERRORS = {}, {}, {}
    # 先摘掉上一轮插件注册的路由，否则热重载会重复挂同一个接口
    if PLUGIN_ROUTES:
        paths = {p for _, p in PLUGIN_ROUTES}
        app.router.routes = [r for r in app.router.routes
                             if getattr(r, "path", None) not in paths]
        PLUGIN_ROUTES.clear()
    if not os.path.isdir(PLUGIN_DIR):
        return
    state = _plugins_state()
    for name in sorted(os.listdir(PLUGIN_DIR)):
        d = os.path.join(PLUGIN_DIR, name)
        man_p = os.path.join(d, "plugin.json")
        if not (os.path.isdir(d) and os.path.isfile(man_p)):
            continue
        try:
            man = json.loads(open(man_p, encoding="utf-8").read())
        except Exception as e:
            PLUGIN_ERRORS[name] = f"plugin.json 解析失败：{e}"
            continue
        man["name"] = man.get("name") or name
        man["dir"] = d
        man["enabled"] = bool(state.get(name, man.get("enabled", True)))
        PLUGINS[name] = man
        if not man["enabled"]:
            continue
        rel = man.get("main")
        if not rel:
            continue
        main_p = os.path.join(d, rel)
        if not os.path.isfile(main_p):
            PLUGIN_ERRORS[name] = f"找不到 {rel}"
            continue
        try:
            mod_name = "twin_plugin_" + re.sub(r"\W", "_", name)
            spec = importlib.util.spec_from_file_location(mod_name, main_p)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            if hasattr(mod, "register"):
                mod.register(PluginAPI(name, d))
        except Exception as e:
            PLUGIN_ERRORS[name] = f"载入出错：{type(e).__name__}: {e}"
    # 通知插件：都载入好了（可以读别人的接口、初始化自己的状态）
    run_hooks("app_start", {"count": len(PLUGINS)})


def run_hooks(hook_name, payload):
    """依次执行钩子；钩子返回 dict 就替换 payload（可改消息、换成员、改界面）"""
    for _, pname, fn in sorted(HOOKS.get(hook_name, []), key=lambda x: x[0]):
        try:
            r = fn(payload)
            if isinstance(r, dict):
                payload = r
        except Exception as e:
            print(f"[插件:{pname}] {hook_name} 出错：{type(e).__name__}: {e}")
    return payload


def handle_command(sid, mode, ai_key, message, extra=None):
    """
    以 / 开头的消息交给 command 钩子。
    插件返回 {"handled": True, "reply": "..."} → 由插件直接作答，不再请求模型。
    返回 None 表示没人接管，照常走 AI。
    """
    if not message or not message.startswith("/"):
        return None
    parts = message.split(None, 1)
    p = {"session_id": sid, "mode": mode, "ai": ai_key,
         "cmd": parts[0][1:], "args": (parts[1] if len(parts) > 1 else ""),
         "message": message}
    if extra:
        p.update(extra)
    r = run_hooks("command", p)
    if isinstance(r, dict) and r.get("handled"):
        return r.get("reply") or ""
    return None


def _system_prompt_for(ai_key, system_override=None):
    """取系统提示；插件可在 system_prompt 钩子里改人格/加规则"""
    base = system_override or SYSTEM_PROMPTS.get(ai_key, "")
    hp = run_hooks("system_prompt", {"ai": ai_key, "prompt": base})
    return hp.get("prompt", base)


def _wrap_before_request(ai_key, model, prov, messages, tools):
    """请求模型前的钩子：可换模型 / 换平台 / 改参数 / 改 messages"""
    hp = run_hooks("before_request", {"ai": ai_key, "model": model, "provider": prov,
                                      "messages": messages, "tools": tools})
    msgs = hp.get("messages", messages)
    if isinstance(msgs, list):
        messages[:] = msgs
    return hp.get("model", model), hp.get("provider", prov), hp.get("tools", tools)


def _wrap_on_tool(ai_key, name, args, ok, result):
    """工具执行后的钩子：可改结果、可标记失败"""
    hp = run_hooks("on_tool", {"ai": ai_key, "name": name, "args": args,
                               "ok": ok, "result": result})
    return bool(hp.get("ok", ok)), hp.get("result", result)


def _wrap_before_send(sid, mode, ai_key, text):
    """回复推到前端前的最后一道加工（翻译 / 加签名 / 排版）"""
    hp = run_hooks("before_send", {"session_id": sid, "mode": mode,
                                   "ai": ai_key, "text": text})
    return hp.get("text", text)


def _ui_from_manifest(name, man):
    """把 plugin.json 的 ui 段翻译成前端能直接用的资源清单"""
    ui = man.get("ui") or {}
    ext = f"/ext/{name}/"

    def _url(v):
        if not v:
            return ""
        v = str(v)
        if v.startswith("http://") or v.startswith("https://") or v.startswith("/"):
            return v
        return ext + v.lstrip("/")

    def _urls(v):
        if not v:
            return []
        if isinstance(v, str):
            v = [v]
        return [_url(x) for x in v if x]

    out = {
        "plugin": name,
        "title": ui.get("title") or "",
        "css": _urls(ui.get("css")),
        "js": _urls(ui.get("js")),
        "cssText": ui.get("cssText") or ui.get("css_text") or "",
        "jsText": ui.get("jsText") or ui.get("js_text") or "",
        "snippets": {},
        "vars": ui.get("vars") or {},
        "bodyClass": ui.get("bodyClass") or ui.get("body_class") or [],
        "favicon": _url(ui.get("favicon")),
        "background": None,
    }
    if isinstance(out["bodyClass"], str):
        out["bodyClass"] = [out["bodyClass"]]
    for slot, v in (ui.get("snippets") or {}).items():
        out["snippets"][slot] = _urls(v)
    # 顶层简写：head → head 插槽；html → body 插槽
    for key, slot in (("head", "head"), ("html", "body")):
        if ui.get(key):
            out["snippets"].setdefault(slot, []).extend(_urls(ui.get(key)))
    bg = ui.get("background")
    if isinstance(bg, str):
        bg = {"image": bg}
    if isinstance(bg, dict) and (bg.get("image") or bg.get("url")):
        out["background"] = {
            "url": _url(bg.get("image") or bg.get("url")),
            "size": bg.get("size", "cover"),
            "position": bg.get("position", "center"),
            "repeat": bg.get("repeat", "no-repeat"),
            "attachment": bg.get("attachment", "fixed"),
            "opacity": bg.get("opacity", 1),
            "blur": bg.get("blur", 0),
            "color": bg.get("color", ""),
        }
    return out


def plugin_ui():
    """汇总所有已启用插件要注入前端的资源（清单 ui 段 + ui_assets 钩子）"""
    items = [_ui_from_manifest(k, v) for k, v in PLUGINS.items()
             if v.get("enabled")]
    hp = run_hooks("ui_assets", {"items": items})
    items = hp.get("items", items) or []
    merged = {"items": items, "plugins": [], "css": [], "js": [],
              "cssText": [], "jsText": [], "snippets": {}, "vars": {},
              "bodyClass": [], "titles": [], "background": None, "favicon": ""}
    for it in items:
        if not isinstance(it, dict):
            continue
        merged["plugins"].append(it.get("plugin", ""))
        merged["css"] += it.get("css") or []
        merged["js"] += it.get("js") or []
        if it.get("cssText"):
            merged["cssText"].append(it["cssText"])
        if it.get("jsText"):
            merged["jsText"].append(it["jsText"])
        for slot, urls in (it.get("snippets") or {}).items():
            merged["snippets"].setdefault(slot, []).extend(urls or [])
        merged["vars"].update(it.get("vars") or {})
        merged["bodyClass"] += it.get("bodyClass") or []
        if it.get("title"):
            merged["titles"].append(it["title"])
        if it.get("background"):          # 有多个插件给背景板时，后加载的生效
            merged["background"] = it["background"]
        if it.get("favicon"):
            merged["favicon"] = it["favicon"]
    return merged


def _plugin_info(k, v):
    ui = v.get("ui") or {}
    return {
        "name": k,
        "title": v.get("title") or k,
        "version": v.get("version", ""),
        "author": v.get("author", ""),
        "description": v.get("description", ""),
        "panel": v.get("panel", ""),
        "main": v.get("main", ""),
        "enabled": v.get("enabled", True),
        "builtin": bool(v.get("builtin")),
        "hooks": sorted({h for h, items in HOOKS.items()
                         for _, n, _ in items if n == k}),
        "ui": ({
            "css": len(ui.get("css") or []),
            "js": len(ui.get("js") or []),
            "background": bool(ui.get("background")),
            "vars": len(ui.get("vars") or {}),
            "snippets": sum(len(v2 if isinstance(v2, list) else [v2])
                            for v2 in (ui.get("snippets") or {}).values()),
        } if ui else None),
        "error": PLUGIN_ERRORS.get(k, ""),
    }


@app.get("/api/plugins")
def api_plugins():
    return JSONResponse({
        "dir": PLUGIN_DIR,
        "guide": PLUGIN_GUIDE,
        "hook_names": HOOK_NAMES,
        "ui_slots": UI_SLOTS,
        "plugins": [_plugin_info(k, v) for k, v in PLUGINS.items()],
    })


@app.get("/api/plugins/ui")
def api_plugins_ui():
    """前端启动/刷新时拉这个：所有插件要注入的 CSS / JS / HTML / 变量 / 背景板"""
    return JSONResponse(plugin_ui())


@app.get("/api/plugins/stream")
async def api_plugins_stream(request: Request):
    """插件 → 前端 的实时事件通道（插件里 api.emit(...) 推的事件在这儿下发）"""
    q = queue.Queue()
    with SUB_LOCK:
        SUBSCRIBERS.append(q)

    async def gen():
        try:
            yield sse({"type": "hello",
                       "plugins": [k for k, v in PLUGINS.items() if v.get("enabled")]})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.to_thread(q.get, True, 15)
                except Exception:
                    yield ": ping\n\n"        # 心跳，保活
                    continue
                yield sse(msg)
        finally:
            with SUB_LOCK:
                if q in SUBSCRIBERS:
                    SUBSCRIBERS.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/plugins/toggle")
async def api_plugins_toggle(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if name not in PLUGINS:
        return JSONResponse({"error": "插件不存在"}, status_code=400)
    st = _plugins_state()
    st[name] = bool(body.get("enabled", True))
    _save_plugins_state(st)
    load_plugins()
    return JSONResponse({"ok": True, "enabled": st[name]})


@app.post("/api/plugins/reload")
def api_plugins_reload():
    load_plugins()
    return JSONResponse({"ok": True, "count": len(PLUGINS),
                         "errors": PLUGIN_ERRORS})


PLUGIN_MAIN_TPL = '''# -*- coding: utf-8 -*-
# __TITLE__ —— 工作台插件（后端扩展）
# 改完在「设置 → 工作台插件 → 重新扫描」即时生效，不用重启服务。
# 完整钩子清单见设置里的「开发指南」。


def register(api):
    # ---- 改所有 AI 的人格 / 加通用规则 ----
    @api.hook("system_prompt")
    def system_prompt(p):
        # p = {"ai": "agent_a", "prompt": "..."}
        # p["prompt"] += "\\n【口头禅】说话结尾带一个 ✨"
        return p

    # ---- 自定义指令：用户发 /__SAFE__ 时由这个插件作答 ----
    @api.hook("command")
    def command(p):
        # p = {"session_id":..., "mode": "single|group", "ai":..., "cmd": "...", "args": "..."}
        if p["cmd"] == "__SAFE__":
            return {"handled": True,
                    "reply": "插件 __SAFE__ 在，收到参数：" + (p["args"] or "（无）")}
        return None

    # ---- 改用户发出的消息 / 换在场成员 ----
    @api.hook("before_message")
    def before_message(p):
        return p

    # ---- 回复推到前端前的最后加工（翻译 / 加签名 / 排版）----
    @api.hook("before_send")
    def before_send(p):
        # p["text"] = p["text"] + "\\n\\n—— 由 __SAFE__ 加工"
        return p

    # ---- AI 说完后：统计、落盘、推给前端 ----
    @api.hook("after_reply")
    def after_reply(p):
        n = api.store.get("count", 0) + 1
        api.store.set("count", n)
        api.emit("reply", {"ai": p["ai"], "count": n})

    # ---- 插件自带接口 ----
    @api.route("GET", "/api/__SAFE__/stats")
    def stats():
        return {"ok": True, "plugin": "__SAFE__", "replies": api.store.get("count", 0)}
'''

PLUGIN_UI_CSS_TPL = '''/* __TITLE__ —— 界面改造（插件 __SAFE__） */
/* 想换背景板：在 plugin.json 的 ui.background 里指一张图，最省事；
   也可以在这里直接用 CSS：
   body{ background:url('bg.svg') center/cover fixed no-repeat; }          */

/* 覆盖主题变量：改这里，整个工作台的配色/圆角/字号一起变
:root{
  --bg:#0b1020;
  --panel:rgba(255,255,255,.72);
  --panel2:rgba(255,255,255,.55);
  --line:rgba(15,23,42,.14);
  --accent:#f0b429;
  --r-msg:14px;
}                                                                    */

/* 只改某一处：按 F12 看一眼元素结构再写选择器 */
'''

PLUGIN_UI_JS_TPL = '''/* __TITLE__ —— 前端行为扩展（插件 __SAFE__）
   可用的全局对象 window.WB 见「设置 → 工作台插件 → 开发指南」 */
(function () {
  if (!window.WB) return;
  WB.toast('插件 __SAFE__ 已加载');

  // 往左侧功能栏加一个图标
  WB.addRailButton({icon: '🎨', title: '__TITLE__', onclick() {
    const box = document.createElement('div');
    box.innerHTML = '<p>这是插件 __SAFE__ 自己的面板。</p>'
                  + '<p>可以在这里放按钮、图表，调用 <code>/api/__SAFE__/stats</code>。</p>';
    WB.openPanel('__TITLE__', box);
  }});

  // 每次有 AI 回复时触发
  WB.on('reply', function (e) { console.log('[__SAFE__] 回复', e); });

  // 收到后端 api.emit("xxx") 推来的事件
  WB.on('plugin:reply', function (e) { console.log('[__SAFE__] 服务端事件', e); });
})();
'''

PLUGIN_PANEL_TPL = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
  body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;padding:18px;color:#1f2430;margin:0}
  h2{margin:0 0 8px;font-size:17px}
  code{background:#f1f3f7;padding:1px 5px;border-radius:4px}
  p{line-height:1.7;font-size:13px}
</style>
</head><body>
<h2>__TITLE__</h2>
<p>这是插件的面板页（<code>/ext/__SAFE__/panel.html</code>），可以放任意 HTML / CSS / JS。</p>
<p>也可以什么都不写——界面改造直接放在 <code>ui.css</code> / <code>ui.js</code> 里更省事。</p>
<p>调工作台接口：<code>fetch('/api/__SAFE__/stats')</code></p>
</body></html>
'''

PLUGIN_README_TPL = '''# __TITLE__

`__SAFE__` —— 工作台插件。

| 文件 | 作用 |
|---|---|
| `plugin.json` | 清单：名称、版本、作者、说明、后端入口、界面改造（`ui` 段） |
| `main.py` | 后端钩子（改人格、改消息、加工回复、自定义 `/指令`、自带接口） |
| `ui.css` | 界面改造：背景板、配色、圆角、字号，任意 CSS |
| `ui.js` | 前端行为扩展，能用 `window.WB` 的全部能力 |
| `panel.html` | 设置里「打开面板」看到的页面 |

改完在「设置 → 工作台插件 → 重新扫描」+「⟳ 重载界面」即时生效，不用重启服务。

完整钩子清单与 `WB` 接口见「设置 → 工作台插件 → 开发指南」。
'''


def _tpl(tpl, safe, title):
    """模板占位符替换（用 __SAFE__/__TITLE__，避免 .format 跟 CSS/JS 的花括号打架）"""
    return tpl.replace("__SAFE__", safe).replace("__TITLE__", title)


@app.post("/api/plugins/create")
async def api_plugins_create(request: Request):
    body = await request.json()
    raw = (body.get("name") or "").strip()
    title = (body.get("title") or raw).strip()
    if not raw:
        return JSONResponse({"error": "请填写插件名（英文/数字/-/_）"}, status_code=400)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", raw).strip("-").lower()
    if not safe:
        return JSONResponse({"error": "插件名只能是英文、数字、- 或 _"}, status_code=400)
    d = os.path.join(PLUGIN_DIR, safe)
    if os.path.exists(d):
        return JSONResponse({"error": f"插件 {safe} 已存在"}, status_code=400)
    os.makedirs(d, exist_ok=True)
    man = {
        "name": safe, "title": title or safe, "version": "1.0.0",
        "author": "", "description": "", "main": "main.py", "panel": "panel.html",
        # ui 段 = 直接改界面：CSS/JS 文件、注入插槽、主题变量、背景板
        "ui": {
            "css": ["ui.css"],
            "js": ["ui.js"],
            "vars": {},
            "snippets": {},
            "bodyClass": [],
            "background": None,
        },
    }
    files = {
        "plugin.json": json.dumps(man, ensure_ascii=False, indent=2),
        "main.py": _tpl(PLUGIN_MAIN_TPL, safe, title or safe),
        "ui.css": _tpl(PLUGIN_UI_CSS_TPL, safe, title or safe),
        "ui.js": _tpl(PLUGIN_UI_JS_TPL, safe, title or safe),
        "panel.html": _tpl(PLUGIN_PANEL_TPL, safe, title or safe),
        "README.md": _tpl(PLUGIN_README_TPL, safe, title or safe),
    }
    for fn, content in files.items():
        with open(os.path.join(d, fn), "w", encoding="utf-8") as fh:
            fh.write(content)
    load_plugins()
    return JSONResponse({"ok": True, "name": safe, "dir": d,
                         "files": sorted(files.keys())})


# ---------------------------------------------------------------------------
# 外部能力：MCP 服务管理 + DSH 插件发现（让工作台直接用别人写好的能力）
# ---------------------------------------------------------------------------
_MCP_CFG_KEYS = ("command", "args", "env", "cwd", "url", "headers", "disabled")


def _mcp_settled():
    """连接状态已是最新：清 dirty 标记并重建工具表"""
    global _MCP_DIRTY
    _MCP_DIRTY = False
    _mcp_sync_tools()


@app.get("/api/mcp")
def api_mcp_list():
    """列出已配置的 MCP 服务（含连接状态与工具清单）+ 本机 DSH 插件扫描结果"""
    cfg = _mcp_cfg()
    servers = []
    for name in sorted(cfg):
        c = cfg[name]
        st = _MCP_PROCS.get(name) or {}
        proc = st.get("proc")
        alive = bool(proc is not None and proc.poll() is None)
        servers.append({
            "name": name,
            "cfg": _mcp_public_cfg(c),
            "disabled": bool(c.get("disabled")),
            "alive": alive,
            "error": str(_MCP_ERR.get(name) or "")[:300],
            "tools": [{"name": t.get("name"), "desc": str(t.get("description") or "")[:120]}
                      for t in (st.get("tools") or [])],
        })
    return {"servers": servers, "dsh": dsh_scan(), "totalTools": len(MCP_TOOLS)}


@app.post("/api/mcp")
def api_mcp_action(body: dict):
    """action: add / update / delete / toggle / test / refresh"""
    b = body or {}
    act = str(b.get("action") or "").strip()
    name = str(b.get("name") or "").strip()
    cfg = dict(_mcp_cfg())

    if act in ("add", "update"):
        c = b.get("cfg") or {}
        if not name:
            return {"ok": False, "msg": "缺少服务名"}
        if not isinstance(c, dict):
            return {"ok": False, "msg": "cfg 必须是对象"}
        keep = {k: v for k, v in c.items() if k in _MCP_CFG_KEYS}
        if not keep.get("command") and not keep.get("url"):
            return {"ok": False, "msg": "至少要有 command（本地命令）或 url（远程服务）"}
        cfg[name] = keep
        SETTINGS["mcpServers"] = cfg
        save_settings({"mcpServers": cfg})
        _mcp_kill(name)
        if keep.get("disabled"):
            _mcp_settled()
            return {"ok": True, "msg": "已保存（停用状态）", "tools": []}
        tools, err = mcp_connect(name, keep)
        if err:
            _MCP_ERR[name] = err
            _mcp_settled()
            return {"ok": True, "msg": "已保存，但连接失败：" + str(err)[:300], "tools": []}
        _MCP_ERR.pop(name, None)
        _mcp_settled()
        return {"ok": True, "msg": "已连接，可用工具 %d 个" % len(tools),
                "tools": [t.get("name") for t in tools]}

    if not name:
        return {"ok": False, "msg": "缺少服务名"}

    if act == "delete":
        cfg.pop(name, None)
        SETTINGS["mcpServers"] = cfg
        save_settings({"mcpServers": cfg})
        _mcp_kill(name)
        _MCP_ERR.pop(name, None)
        _mcp_settled()
        return {"ok": True, "msg": "已删除"}

    if act == "toggle":
        if name not in cfg:
            return {"ok": False, "msg": "没有这个服务"}
        cfg[name]["disabled"] = bool(b.get("disabled"))
        SETTINGS["mcpServers"] = cfg
        save_settings({"mcpServers": cfg})
        if cfg[name]["disabled"]:
            _mcp_kill(name)
        else:
            tools, err = mcp_connect(name, cfg[name])
            if err:
                _MCP_ERR[name] = err
                _mcp_settled()
                return {"ok": True, "msg": "已启用，但连接失败：" + str(err)[:300]}
            _MCP_ERR.pop(name, None)
        _mcp_settled()
        return {"ok": True, "msg": "已更新"}

    if act == "test":
        c = cfg.get(name)
        if not c:
            return {"ok": False, "msg": "没有这个服务"}
        _mcp_kill(name)
        tools, err = mcp_connect(name, c)
        if err:
            _MCP_ERR[name] = err
            _mcp_settled()
            return {"ok": False, "msg": "连接失败：" + str(err)[:300], "tools": []}
        _MCP_ERR.pop(name, None)
        _mcp_settled()
        return {"ok": True, "msg": "连接成功，工具 %d 个" % len(tools),
                "tools": [t.get("name") for t in tools]}

    if act == "add-demo":
        demo = os.path.join(BASE_DIR, "mcp_demo", "server.py")
        if not os.path.isfile(demo):
            return {"ok": False, "msg": "示例服务文件不存在：" + demo}
        return api_mcp_action({"action": "add", "name": "demo", "cfg": {
            "command": "python", "args": [demo],
            "cwd": os.path.join(BASE_DIR, "mcp_demo")}})

    if act == "refresh":
        mcp_refresh()
        _mcp_settled()
        return {"ok": True, "msg": "已重连，当前 MCP 工具 %d 个" % len(MCP_TOOLS),
                "tools": [t["function"]["name"] for t in MCP_TOOLS]}

    return {"ok": False, "msg": "未知 action：" + act}


@app.post("/api/mcp/adopt-dsh")
def api_mcp_adopt_dsh(body: dict):
    """把扫描到的 DSH 插件（限 MCP 形态）一键接入工作台"""
    b = body or {}
    name = str(b.get("name") or "").strip()
    found = None
    for p in dsh_scan():
        if p["name"] == name:
            found = p
            break
    if not found:
        return {"ok": False, "msg": "没找到这个 DSH 插件"}
    if found["kind"] != "mcp":
        return {"ok": False, "msg": "该插件不是 MCP 形态，无法桥接：" + found["reason"]}
    cmd = b.get("cfg") or found.get("mcpCmd")
    if not cmd:
        return {"ok": False, "msg": "猜不出它的启动命令，请手动填写"}
    return api_mcp_action({"action": "add", "name": name, "cfg": cmd})


# 静态文件（前端）
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
# 插件静态资源：/ext/<插件名>/panel.html、ui.css、bg.svg、图片视频等任意文件
os.makedirs(PLUGIN_DIR, exist_ok=True)
app.mount("/ext", StaticFiles(directory=PLUGIN_DIR), name="ext")

# 启动即扫描并载入插件（改完可在设置里点「重新扫描」热加载）
load_plugins()

if __name__ == "__main__":
    import uvicorn
    print(f"多 AI 工作台启动中 → http://localhost:{PORT}")
    uvicorn.run(app,
            host=os.environ.get("WORKBENCH_HOST", "127.0.0.1"),
            port=PORT, log_level="info")
