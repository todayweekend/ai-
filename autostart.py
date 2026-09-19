# -*- coding: utf-8 -*-
"""
开机自启管理
============
在 Windows「启动」文件夹放一个 vbs，用 pythonw.exe 静默拉起 app.py（无黑窗）。
这样开机/重启登录后工作台自动可用，不需要手动双击 start.bat。

用法：
  python autostart.py install   安装开机自启
  python autostart.py remove    取消开机自启
  python autostart.py status    查看当前状态
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
STARTUP = os.path.join(os.environ.get("APPDATA", ""),
                       r"Microsoft\Windows\Start Menu\Programs\Startup")
VBS = os.path.join(STARTUP, "TwinAIWorkbench.vbs")
PYW = os.path.join(BASE, "venv", "Scripts", "pythonw.exe")
APP = os.path.join(BASE, "app.py")

# vbs 里的引号需要写成两个双引号
Q = '""'
VBS_CONTENT = (
    'Set ws = CreateObject("WScript.Shell")\n'
    f'ws.CurrentDirectory = "{BASE}"\n'
    f'ws.Run "{Q}{PYW}{Q} {Q}{APP}{Q}", 0, False\n'
)

def install():
    if not os.path.isfile(PYW):
        print(f"[失败] 找不到 {PYW}，无法安装自启。")
        return 1
    if not os.path.isdir(STARTUP):
        print(f"[失败] 启动文件夹不存在：{STARTUP}")
        return 1
    # 自启前先等一会儿，避免开机瞬间磁盘/网络还没就绪
    with open(VBS, "w", encoding="ascii") as f:
        f.write(VBS_CONTENT)
    print(f"[成功] 已安装开机自启：{VBS}")
    print("       开机登录后会自动启动工作台（无窗口），稍等几秒访问 http://localhost:7860")
    return 0

def remove():
    if os.path.isfile(VBS):
        os.remove(VBS)
        print(f"[成功] 已取消开机自启（删除 {VBS}）")
    else:
        print("[信息] 当前没有安装开机自启。")
    return 0

def status():
    if os.path.isfile(VBS):
        print(f"[状态] 已开启开机自启：{VBS}")
        print(f"       启动命令：{PYW} {APP}")
    else:
        print("[状态] 未开启开机自启（需手动双击 start.bat）")
    return 0

if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    fn = {"install": install, "remove": remove, "status": status}.get(cmd)
    if not fn:
        print("用法：python autostart.py install|remove|status")
        sys.exit(1)
    sys.exit(fn())
