# 工作台插件手册

放一个文件夹 = 一个插件。**不用改核心代码**，就能扩展、甚至重做工作台：
界面（背景板 / 配色 / 圆角 / 布局 / 按钮）、回复方式（人格 / 模型 / 文案加工 / 自定义指令）、
自定义接口与实时推送。

改完在「设置 → 工作台插件」点 **⟳ 重载插件 + 界面** 即时生效，无需重启服务。

## 一个插件长什么样

```
plugins/
└─ 你的插件名/
   ├─ plugin.json     清单（必须）
   ├─ main.py         后端钩子 + 自定义接口（可选）
   ├─ ui.css / ui.js  界面改造（可选）
   ├─ panel.html      设置里「打开面板」看到的页面（可选）
   ├─ snippets/       要塞进界面某处的 HTML 片段（可选）
   └─ bg.svg 等素材   通过 /ext/<插件名>/xxx 访问（可选）
```

## plugin.json

```json
{
  "name": "my-skin",
  "title": "我的皮肤",
  "version": "1.0.0",
  "author": "你",
  "description": "换个背景板，顺手改配色",
  "enabled": true,
  "main": "main.py",
  "panel": "panel.html",
  "ui": {
    "css": ["ui.css"],
    "js": ["ui.js"],
    "cssText": "body{--accent:#e0a52a}",
    "jsText": "console.log('内联 JS 也能跑')",
    "snippets": {"settings": ["snippets/settings.html"], "rail": ["snippets/rail.html"]},
    "vars": {"--accent": "#e0a52a", "--r-msg": "15px"},
    "bodyClass": ["my-skin"],
    "background": {"image": "bg.svg", "size": "cover", "opacity": 1, "blur": 0},
    "title": "我的工作台",
    "favicon": "icon.svg"
  }
}
```

| 字段 | 作用 |
|---|---|
| `css` / `js` | 注入样式表与脚本；写相对路径即可（自动变成 `/ext/<插件名>/...`） |
| `cssText` / `jsText` | 直接内联的 CSS / JS 文本 |
| `vars` | 覆盖前端 CSS 变量：`--bg --panel --panel2 --line --text --muted --accent --user-bg --r-msg --fs --lh --mw …` |
| `bodyClass` | 往 `<body>` 加类名，方便在 CSS 里写 `body.my-skin .xxx{}` |
| `snippets` | 把 HTML 片段塞进插槽（**片段里的 `<script>` 会执行**） |
| `background` | 背景板图像层：`image / size / position / repeat / attachment / opacity / blur / color` |
| `title` / `favicon` | 改页面标题 / 图标 |
| `enabled` | 默认是否启用（用户在设置里切换后以用户选择为准） |

**可用插槽**：`head` `body` `top` `bottom` `rail`（左侧功能栏）`clist`（聊天列表）
`chat` `chatbar`（输入框那一行）`settings`（设置面板）`left` `right`

> 背景板是负层级的一层图，要让它露出来，得让面板半透明 ——
> 在 `ui.css` 里覆盖 `--panel` / `--panel2` / `--bg:transparent` 即可（见 `example-skin`）。

## main.py：11 个钩子

```python
def register(api):
    @api.hook("system_prompt")
    def sp(p):                      # 改所有 AI 的人格 / 通用规则
        p["prompt"] += "\n【口头禅】结尾带一个 ✨"
        return p

    @api.hook("command")
    def cmd(p):                     # 自定义指令：/天气 xxx
        if p["cmd"] == "天气":
            return {"handled": True, "reply": "今天晴。"}
        return None                 # 不接管就返回 None
```

| 钩子 | 触发时机 | payload | 能改什么 |
|---|---|---|---|
| `app_start` | 插件全部载入完成 | `{count}` | 初始化自己的状态 |
| `settings` | 前端读设置 | `{settings}` | 锁主题、强制字号… |
| `agents` | 前端读成员 | `{agents}` | 改名 / 换色 / 隐藏 / 加字段 |
| `ui_assets` | 前端拉界面资源 | `{items}` | 动态生成 CSS/JS/插槽/背景板 |
| `system_prompt` | 构造系统提示 | `{ai, prompt}` | 改人格、加规则 |
| `before_message` | 用户消息进来 | `{session_id, mode, message, agents}` | 改消息、换在场成员 |
| `command` | 消息以 `/` 开头 | `{session_id, mode, ai, cmd, args}` | 返回 `{"handled":True,"reply":"..."}` 直接作答 |
| `before_request` | 每次请求模型前 | `{ai, model, provider, messages, tools}` | 换模型 / 换平台 / 改参数 |
| `on_tool` | 工具执行完 | `{ai, name, args, ok, result}` | 改结果、标记失败 |
| `after_reply` | 某个 AI 说完 | `{session_id, mode, ai, text}` | 改落库文本、统计、通知 |
| `before_send` | 回复推到前端前 | `{session_id, mode, ai, text}` | 最终文案加工（会覆盖气泡） |

- 钩子可加 `priority`（默认 50，小的先跑）：`@api.hook("before_send", priority=10)`。
- 返回 `dict` = 用返回值替换 payload；返回 `None` = 不改。
- 抛异常不会搞挂服务，会打印在服务端日志。

## api 对象

| 方法 | 作用 |
|---|---|
| `api.hook(名, priority=50)` | 注册钩子 |
| `api.route("GET", "/api/x")` | 注册接口（不能覆盖工作台已有的「路径 + 方法」） |
| `api.store.get/set/all/update/delete` | 插件私有存储，落在本目录 `.data.json`，重启不丢 |
| `api.settings()` / `api.set_settings({...})` | 读写工作台设置 |
| `api.agents()` | 读全部成员 |
| `api.emit("事件名", {...})` | 实时推给所有在线前端 → `WB.on('plugin:事件名')` |
| `api.path("a.png")` / `api.url("a.png")` | 插件目录的绝对路径 / 可访问地址 |
| `api.shared_path("x.md")` | 共享记忆目录（`shared/`）下的文件路径 |
| `api.log(...)` | 打印到服务端日志（带 `[插件:名字]` 前缀） |

## 前端 window.WB（在 ui.js 里用）

| 方法 | 作用 |
|---|---|
| `WB.settings()` / `WB.patchSettings({...})` | 读 / 改设置（改完立即生效并保存） |
| `WB.agents()` | 成员列表 |
| `WB.toast("文字")` | 右下角提示 |
| `WB.addRailButton({icon, title, onclick})` | 往左侧功能栏加图标 |
| `WB.addSettingSection("标题", fn)` | 往设置面板加一节 |
| `WB.slot("rail")` | 取插槽容器，自己往里塞元素 |
| `WB.on("reply" \| "user" \| "tool" \| "who" \| "ui", fn)` | 事件总线 |
| `WB.on("plugin:事件名", fn)` | 收到后端 `api.emit` 推来的事件 |
| `WB.openPanel("标题", DOM元素)` | 弹自定义面板窗 |
| `WB.send("文字")` | 以当前会话替用户发一条消息 |
| `WB.reloadUI()` | 重新拉插件界面资源 |

## 现成例子

- `example-stats/` —— 发言统计：钩子记账 + 自带接口 + 面板画柱状图。
- `example-skin/` —— 星轨夜空皮肤：背景板 + 配色 + 功能栏按钮 + 设置项 +
  回复签名 + `/skin` 指令，一个插件把界面和回复方式全改了一遍。

复制它们改成自己的，是最快的上手方式。

## 注意

插件在服务进程内运行，**等同于本机代码权限**：只装你自己信得过（或看得懂）的插件。
停用某个插件：设置 → 工作台插件 → 停用（状态记在 `.plugins_state.json`）。

### 写 POST 接口的两个坑

```python
from fastapi import Request          # ① 必须 import

@api.route("POST", "/api/x")
async def save(request: Request):    # ② 参数必须标注 Request
    body = await request.json()
```

- **不标注 `Request`** → FastAPI 把它当成必填查询参数 → 前端收到 **422**。
- 同一个路径的 GET 和 POST 是**两条独立路由**，可以放心各注册一个
  （早期版本只按路径去重，POST 会被当成重复吞掉 → **405**，已修）。

