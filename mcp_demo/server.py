# -*- coding: utf-8 -*-
"""
最小可用的 MCP server 示例（stdio + JSON-RPC 2.0，纯标准库，无需装任何包）。

作用：
1. 验证工作台的 MCP 客户端确实能连上、能拿到工具、能调用；
2. 当模板用 —— 照着这个结构改，就能把任何脚本/接口包装成 AI 可用的工具。

运行方式（由工作台自动拉起，一般不用手动跑）：
    python server.py
协议：stdin 收一行 JSON，stdout 回一行 JSON（newline-delimited JSON）。
"""
import json
import sys
import datetime

TOOLS = [
    {
        "name": "demo_echo",
        "description": "把收到的文字原样返回，用来测试连通性。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文字"}},
            "required": ["text"],
        },
    },
    {
        "name": "demo_time",
        "description": "返回当前本机时间（YYYY-MM-DD HH:MM:SS）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "demo_add",
        "description": "两个数字相加，返回计算结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "第一个数"},
                "b": {"type": "number", "description": "第二个数"},
            },
            "required": ["a", "b"],
        },
    },
]


def handle(req):
    m = req.get("method")
    i = req.get("id")
    p = req.get("params") or {}

    if m == "initialize":
        return {"jsonrpc": "2.0", "id": i, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "twin-ai-demo", "version": "1.0"},
        }}

    if str(m or "").startswith("notifications/"):
        return None

    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": i, "result": {"tools": TOOLS}}

    if m == "tools/call":
        n = p.get("name")
        a = p.get("arguments") or {}
        if n == "demo_echo":
            txt = "收到：" + str(a.get("text") or "")
        elif n == "demo_time":
            txt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elif n == "demo_add":
            try:
                txt = str(float(a.get("a")) + float(a.get("b")))
            except Exception as ex:
                txt = "参数错误：%r" % (ex,)
        else:
            return {"jsonrpc": "2.0", "id": i,
                    "error": {"code": -32601, "message": "未知工具 " + str(n)}}
        return {"jsonrpc": "2.0", "id": i,
                "result": {"content": [{"type": "text", "text": txt}], "isError": False}}

    if m == "ping":
        return {"jsonrpc": "2.0", "id": i, "result": {}}

    return {"jsonrpc": "2.0", "id": i,
            "error": {"code": -32601, "message": "未知方法 " + str(m)}}


def main():
    for line in sys.stdin:
        line = (line or "").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
