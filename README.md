# 多 AI 工作台（Multi-AI Workbench）

一个**跑在自己电脑上的多 AI 协作工作台**。把 N 个 AI 放进同一个网页里：
单聊、群聊（多 AI 同场混着说）、各自带工具真正干活、共享记忆、界面随便改。

后端 FastAPI + 原生前端 + SSE 流式，**零构建、零前端框架**，克隆下来配个 key 就能跑。

> 💬 **QQ 交流群：874924265** —— 装不上、配不通、想写插件卡住了，都可以进来问；
> 也欢迎在里面提需求、报 bug、晒你自己的分身。

```
┌─────────┬──────────────┬──────────────────────────────────────┐
│ 功能栏  │  聊天列表    │  聊天窗口                            │
│  💬 ☰  │  AI 成员    │  QQ 风格气泡 · 流式打字 · 工具卡片   │
│  🌌…   │  群聊        │  群聊顶栏「⚡全员必答」开关          │
└─────────┴──────────────┴──────────────────────────────────────┘
```

## 特性

- **N 个成员，随便加** —— 在设置里增删 AI，每个成员可以是任意
  **OpenAI 兼容接口**（DeepSeek / GPT / Kimi / GLM / 通义 / 本地 Ollama…），
  各带自己的 key、模型、人设文件、工作区。
- **开箱是一张白纸，不带任何人的密钥** —— 第一次打开是空工作台，页面直接引着你
  「添加第一个 AI」；谁没配好接口，界面上标得明明白白，缺 Key 时给的是可操作的提示
  而不是一个报错。把项目打包发给别人，对方填自己的 Key 就能跑。
- **真·工具 agent（内置 12 个工具）** —— 读文件 / 写文件 / **精准编辑** /
  移动改名 / 读指定行 / 列目录 / **递归搜索工作区** / 跑命令 /
  检索与追加共享记忆 / **联网搜索** / **抓取网页正文**
  （OpenAI 函数调用实现，含流式工具卡片）。完整清单见 [工具一览](#工具一览)。
- **两种工作引擎，可以混着用** —— 默认走内置的接口引擎（带上面那套工具）；
  也可以把某个成员换成**外部命令引擎**：把话交给一条本地命令，取它的 stdout 当回复。
  DeepSeek Harness、Claude Code、Codex、你自己写的脚本，都能挂上当一个成员。
- **群聊** —— 多个 AI 同场混着说：点名必回、没点名各自判断该不该接话、
  可设「组长 / 平权 / 旁听」，你随时插话打断；也可开「全员必答」。
- **共享记忆 + 命名铁律** —— 所有 AI 共用一个目录，每人只写自己前缀的文件
  （代码层强制），跨 AI 交接不打架。
- **插件系统：能改工作台的一切** —— 界面（背景板 / 配色 / 圆角 / 按钮 / 布局）、
  回复方式（人格 / 模型 / 文案加工 / 自定义指令）、自定义接口与实时推送，
  **全都不用动核心代码**，改完点一下即时生效。详见 [`plugins/README.md`](plugins/README.md)。
- **全本地、默认不对外** —— 聊天记录、设置、记忆都是本地文件；服务默认只监听
  `127.0.0.1`，同一局域网里的其他设备连不上（因为这工作台的 `run_command`
  会在这台机器上真执行命令）。要开放给手机 / 局域网，见
  [开放给局域网](#开放给局域网)。
- **过程可见（看 AI 怎么干活）** —— 工具卡片带「耗时」；一轮结束给一条用量汇总
  （token 数 / 入出 / 步数 / 耗时 / 模型，provider 不返回用量时按字符数估算并标「约」）；
  右侧「📂 产出文件」抽屉列出 AI 这轮写出的所有文件，点开即预览 / 下载。
  灵感来自云端科研 Agent（把「聊天」变成「交付」的过程展示）。

## 快速开始

```bash
git clone https://github.com/todayweekend/ai-.git
cd ai-
python -m venv venv
# Windows
venv\Scripts\pip install -r requirements.txt
# macOS / Linux
venv/bin/pip install -r requirements.txt

# Windows
start.bat
# 其它平台
venv/bin/python app.py
```

打开 <http://localhost:7860>。

**第一次启动会自动生成一份空的 `config.json`**（没有成员、没有任何密钥）。
页面上会引导你「＋ 添加第一个 AI」：填个名字，再填上你自己的接口地址和 API Key，就能开聊。

`start.bat` 会先跑 `check.py` 自检（依赖 / key / 连通性 / 端口 / 目录），
有问题直接告诉你哪儿不对，而不是让你对着黑窗口发呆。停止用 `stop.bat`。

## 配置

`config.json`（**不要提交到 git**，已在 `.gitignore` 里）。
它在第一次启动时自动生成，内容取自 `config.example.json` —— 一份**空配置**，
不含任何密钥，所以把整个项目打包发给别人不会泄露你的 Key。
界面里「设置 → AI 成员与模型」改的就是它：

```jsonc
{
  "deepseek": {                     // 默认接口（没单独配 provider 的成员走这里）
    "api_key": "",
    "model": "deepseek-flash",
    "base_url": "https://api.deepseek.com/v1"
  },
  "agents": [                       // 成员列表，想加几个加几个；可以是空的
    {
      "key": "assistant",           // 内部标识（英文）
      "name": "助手",                // 显示名，群聊里用它点名
      "emoji": "🤖",
      "color": "#2563eb",
      "prefix": "AS_",              // 共享记忆里它只能写这个前缀的文件
      "workspace": "./ws_assistant",          // 它的工作区（工具只能在这里读写）
      "persona_files": [],          // 人设文件，可多个；文件不存在会自动跳过，不报错
      "provider": {                 // 可选：这个成员单独走别的平台（任意 OpenAI 兼容）
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": "sk-...",
        "model": "kimi-k2.6"
      },
      "engine": {                   // 可选：换掉内置引擎，改把话交给一条本地命令
        "type": "command",
        "cmd": ["claude", "-p", "{prompt}"],
        "cwd": "./ws_engine",
        "timeout": 300
      }
    }
  ],
  "shared_memory": "./shared",      // 所有 AI 共用的记忆目录
  "sessions_dir": "./sessions",     // 聊天记录
  "port": 7860,
  "max_tool_rounds": 8              // 单次回答里最多几轮工具调用
}
```

同一个 AI 的配置全都集中在**一个弹窗**里：名字、头像、身份色、人设文件、工作区、
记忆前缀、群聊身份、接口地址、API Key、模型名 —— 一次填完，不用满页面找。

`settings.json`（外观 / 文字 / 会话偏好）由界面自动生成，与放 key 的 config 分开。

## 各平台的接口地址和模型名（照抄即可）

**最容易踩的坑**：模型名必须填服务商的**真实 ID**，自己写简称或带错横线一律失败。
比如 Moonshot 是 `kimi-k2.6`，写 `K2.6` 或 `kimi-k2` 都会被拒。

| 服务商 | base_url | 模型名 | 备注 |
|---|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-flash`、`deepseek-v4-pro` | 内置默认 |
| Kimi / Moonshot | `https://api.moonshot.cn/v1` | `kimi-k2.6`、`kimi-k2.5`、`kimi-k3` | 国内站；国际站是 `api.moonshot.ai` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.6`、`glm-4-flash` | |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-max`、`qwen-plus` | 注意路径里的 `compatible-mode` |
| 豆包 / 火山方舟 | `https://ark.cn-beijing.volces.com/api/v3` | 填控制台的**接入点 ID**（`ep-` 开头） | 填模型名会失败 |
| 腾讯混元 | `https://api.hunyuan.cloud.tencent.com/v1` | `hunyuan-turbos-latest` 等 | 以控制台为准 |
| 本地 Ollama | `http://localhost:11434/v1` | 你本地拉的模型名 | Key 随便填，但不能留空 |

**拿不准就直接问服务商**，它会把这个 Key 能用的模型全列出来：

```bash
curl https://api.moonshot.cn/v1/models -H "Authorization: Bearer 你的Key"
```

凡是兼容 OpenAI `/chat/completions` 的服务都能接 —— 上面只是常用的几家，
聚合平台（硅基流动、OpenRouter）、公司自建网关、本地 vLLM 都一样能填。

> 自检脚本 `check.py` 会**逐个成员**实测接口。Key 失效或模型名不对只会记一条警告，
> **不会阻止程序启动** —— 你照常打开工作台，进去在「设置 → AI 成员与模型」里改就行。

## 工作引擎：内置的够用，也可以换成你自己的 CLI

每个成员都可以选一种"工作方式"（在成员编辑弹窗里改，对应 `config.json` 的 `engine` 字段）：

| 引擎 | 它怎么干活 | 需要什么 |
|---|---|---|
| **接口引擎**（默认） | 工作台内置的 agent 循环：读人设文件 → 带 key 调 OpenAI 兼容接口 → 12 个工具（清单见 [工具一览](#工具一览)），最多 8 轮 | 一个 API Key |
| **外部命令引擎** | 把最近几轮对话拼成一段文本交给一条本地命令，**取它的 stdout 当回复** | 本机装了那个命令 |

外部命令引擎的规则很简单：

- 命令里写 `{prompt}` → 那段话作为**命令行参数**传进去（例：`claude -p "{prompt}"`）
- 命令里不写 → 那段话从 **stdin** 喂进去（例：`ollama run llama3.2`）

两种风格都支持，所以 DeepSeek Harness、Claude Code、Codex、Gemini CLI、本地 Ollama，
甚至你自己写的十行脚本，都能变成一个成员。

点成员弹窗里的「探测本机装了哪些」，它会扫一遍 PATH 把找到的 CLI 列出来，点一下自动填好。
还能单独给某个成员配工作目录、超时、环境变量。

> ⚠️ 这条命令会在你电脑上**直接执行**（跟你自己开终端敲一遍一样），**没有二次确认** ——
> 只填你自己信得过的。

## 工具一览

内置的**接口引擎**给每个成员 12 个工具。目标很朴素：让 AI 能像人一样
「找文件 → 读一段 → 改一处 → 跑一下验证」，而不是只会整篇覆盖。

| 工具 | 它能做什么 |
|---|---|
| `read_file` | 读整个文件（很大时按上限截断，配合 `read_lines` 用） |
| `read_lines` | 只读指定行区间 —— 大文件 / 长代码里精准定位 |
| `write_file` | 写一个新文件（整篇覆盖） |
| `edit_file` | **把文件里某段原文精准替换成新文本** —— 改一部长文件的一小处，不用整篇重写 |
| `move_rename_file` | 移动 / 重命名文件（跨目录也行） |
| `list_dir` | 列一层目录 |
| `search_workspace` | 在自己的工作区里**递归搜索**（按内容或文件名），可限定扩展名 |
| `run_command` | 跑一条本机命令，取它的输出 |
| `search_memory` | 在共享记忆区里搜关键词 |
| `append_memory` | 往自己的共享记忆文件里追加一段（只能写自己前缀的文件） |
| `web_search` | 联网搜索，拿回标题 / 链接 / 摘要 |
| `fetch_url` | 抓一个网页的正文（自动去掉 HTML 标签） |

几条约定：

- **工具只在自己的工作区里动手**：每个成员有自己的 `workspace` 目录，越界的
  路径会被拒绝；Windows 非法字符与保留设备名也一律拦下并给出改名建议。
- **共享记忆有命名铁律**：每个成员只能写 `<自己的前缀>*.md`，代码层强制，
  多个 AI 交替干活不会互相覆盖。
- **`run_command` 有硬超时**：跑飞 / 卡住的命令会被连同子进程一起杀掉，
  不会把整个服务拖死。
- **联网工具只用标准库**，不引额外依赖；搜索源用 Bing（国内可直连）。

> 工具是可选的：模型不调用工具时就是普通聊天。要让它真的动手，
> 在话里说清「哪个文件、改成什么」即可。

## 造一个你自己的分身

工作台里的 AI 之所以"像某个具体的家伙"，靠的不是模型，而是**身份文件**：

```
personas/
├─ IDENTITY.md   它是谁（名字 / 形态 / 调性）
├─ SOUL.md       它怎么做事（性格 / 边界 / 说话方式）
└─ USER.md       它在跟谁说话（你 / 你的习惯 / 你的硬性要求）
```

工作台每次开口前，会把这些文件的全文拼进系统提示。**所以改文件 = 它下次说话就变了** ——
不用重启服务，不用重新训练。

三步得到一个带人格的分身：

1. 把上面三份模板填成你自己的（一份不填也行，那就只是个通用助手）
2. **设置 → AI 成员与模型 → 「⇪ 导入身份文件」**，选中填好的几份
3. 回到它那一行点「编辑」，填上接口地址 / API Key / 模型名

名字会从 `IDENTITY.md` 里的 `Name:` 自动认出来。导入的文件会**复制进**
`personas/<成员标识>/`，所以整个项目文件夹是自包含的 —— 换台机器照样读得到。

## 过程可见性（看 AI 怎么干活）

借鉴云端科研 Agent「把聊天变成交付」的思路，把工作台从「看 AI 说话」往「看 AI 干活」推一格。
所有信息都来自同一份数据（每个工具调用的时间戳、产出文件、token 消耗），展示层改动，不动内核。

- **步骤耗时**：每个工具卡片下方显示该步耗时（`Xs`），让你知道 AI 卡在哪一步。
- **用量汇总**：一轮对话结束，气泡流末尾追加一条用量条 —— `总 tokens / 入 / 出 / 步数 / 耗时 / 模型`。
  - 依赖 `stream_options.include_usage`（DeepSeek 支持）。若用的模型 / 接口不返回用量，
    自动按字符数估算并标「约 / 估算」，绝不留白。
- **产出文件抽屉**：聊天窗口右侧「📂 产出文件」按钮展开侧栏，按工作区分组列出 AI 写出的文件，
  文本 / 图片直接预览，其余给下载。数据来自 `/api/ws-files`（只暴露成员工作区与 uploads，越权 403）。

> 实现位置：后端 `run_agent_stream` 包装层累计 token 与步骤、`/api/ws-files` 列文件；
> 前端 `addUsage` / `loadFileTree` / 工具卡片 `.tdur`。

## 外部能力（MCP）：直接用别人写好的工具

**MCP（Model Context Protocol）是 AI 工具的通用标准。** 社区里有大量现成的 MCP 服务
（文件系统、搜索、数据库、GitHub、浏览器、Notion…）。接上任意一个，它的工具会**立刻
变成 AI 可以调用的工具** —— 不用写插件，也不用改代码。

在 **设置 → 外部能力** 里填一行启动命令就行，例如：

```
npx -y @modelcontextprotocol/server-filesystem 某个本地目录的绝对路径
```

点「添加并连接」，成功后会列出这个服务提供的工具。之后你在聊天里直接说"帮我看看
Documents 里有什么"，AI 就会自己去调。

配置写在 `settings.json` 的 `mcpServers` 里：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\me\\Documents"],
      "cwd": "",
      "env": {},
      "disabled": false
    }
  }
}
```

**想自己写一个 MCP 服务？** 看 `mcp_demo/server.py` —— 一个不到 150 行、纯标准库
的最小实现（stdio + JSON-RPC 2.0），照着改就能把任何脚本或接口包装成 AI 可用的工具。
设置页里的「⚡ 接入示例服务」按钮就是把它接进来验证链路用的。

### 关于 DeepSeek Harness（DSH）插件

工作台会**自动扫描**你本机的 DSH 插件（`~/.dsh/profiles/*/node_modules` 等目录），
并判断它能不能接进来：

| 类型 | 判定 | 处理 |
|---|---|---|
| MCP 形态 | `可接入` | 直接「⇪ 接入工作台」，工具立刻可用 |
| Cordis 界面挂件 | `界面挂件` | **接不了** —— 它修改的是 DSH 自己的界面，属于"缝在 DSH 身上的一块肉"。想要同款功能请用「工作台插件」重写，反而更简单 |
| 普通 Node 包 | `普通包` | 需人工判断用途 |

不建议为了跑 DSH 插件而在工作台里内嵌 Node 运行时 + Cordis 框架 —— 技术上做得到，
但性价比是负的。有现成的 MCP 服务可用时，直接用 MCP 就好。

## 插件系统

一个文件夹 = 一个插件，**能改工作台的一切**：

| 能改什么 | 靠什么 |
|---|---|
| 背景板图像、配色、圆角、字号 | `plugin.json` 的 `ui` 段 + `ui.css` |
| 功能栏图标、设置面板、任意位置的 HTML | `ui.js` 里的 `window.WB.addRailButton / addSettingSection / slot` |
| AI 的人格与规则 | `system_prompt` 钩子 |
| 换模型 / 换平台 / 改请求参数 | `before_request` 钩子 |
| 最终回复文案（翻译 / 加签名 / 排版） | `before_send` 钩子 |
| 自定义 `/指令` | `command` 钩子 |
| 自己的接口、自己的存储、实时推送前端 | `api.route` / `api.store` / `api.emit` |

共 11 个钩子 + 前端 `window.WB` 接口，完整清单见
[`plugins/README.md`](plugins/README.md)；
现成例子：`plugins/example-skin/`（星轨夜空皮肤：背景板 + 配色 + 按钮 +
设置项 + 回复签名 + `/skin` 指令）、`plugins/example-stats/`（发言统计）、
`plugins/whale-widget/`（右下角余额挂件：多账户轮换 + 服务端拉余额 +
定时刷新，Key 只留在服务端、不下发前端）。

> 插件在服务进程内运行，等同于本机代码权限 —— 只装你自己信得过（或看得懂）的插件。

## 目录结构

```
├─ app.py                 后端（FastAPI + 工具循环 + SSE 流式 + 插件系统）
├─ static/index.html      前端（单文件，原生 HTML/CSS/JS）
├─ config.example.json    配置模板（首次启动照它生成空的 config.json）
├─ requirements.txt       运行依赖（fastapi / uvicorn）
├─ check.py               启动自检
├─ start.bat / stop.bat   Windows 一键启停
├─ autostart.py           开机自启管理
├─ SECURITY.md            安全模型与漏洞上报方式
├─ CONTRIBUTING.md        参与开发（跑测试 / 提 PR）
├─ CHANGELOG.md           变更记录
├─ .github/workflows/     CI：推上去自动跑下面两个测试
├─ plugins/               工作台插件（含手册与示例）
├─ personas/              身份文件模板（分身配方：填好导进去就有魂）
├─ tests/test_plugins.py  插件系统端到端测试
├─ tests/test_engine.py   工作引擎与分身配方测试
├─ shared/                各 AI 的共享记忆区
├─ sessions/              聊天记录（.json 机器用 + .md 人读）
└─ workspaces/            各成员的工作区
```

## 测试

```bash
venv\Scripts\python.exe tests\test_plugins.py
venv\Scripts\python.exe tests\test_engine.py
```

前者会真实起一遍接口、开关插件、发一条 `/指令`，逐项校验插件系统的每一层；
后者会真起一条子进程当引擎，验证参数 / stdin 两种传法、超时与失败降级、
以及"导入身份文件建成员"这条链路。

## 开放给局域网

发行版默认只监听 `127.0.0.1` —— **只有这台机器能访问**。这是刻意保守的默认值：
这个工作台带 `run_command`，能在这台机器上真执行命令。

想让手机 / 平板 / 另一台电脑连上，把监听地址换成 `0.0.0.0`：

```bat
:: Windows —— 先设变量、再启动，这两条必须在同一个命令行窗口里执行。
:: 直接双击 start.bat 是不行的：双击出来的那个窗口里没有这个变量。
set WORKBENCH_HOST=0.0.0.0
start.bat
```

```bash
# macOS / Linux
WORKBENCH_HOST=0.0.0.0 venv/bin/python app.py
```

> PowerShell 里的写法是 `$env:WORKBENCH_HOST="0.0.0.0"`，同样是「先设、再启动」。

Windows 上最稳的写法是不依赖 `start.bat`，直接自己拉起来：

```bat
set WORKBENCH_HOST=0.0.0.0
venv\Scripts\python app.py
```

不想每次都设环境变量的话，也可以直接改 `app.py` 最后一行 `uvicorn.run(...)`
里的 `host=` 参数 —— 改完固定生效。

改完之后，用 `ipconfig`（Windows）/ `ifconfig`（macOS、Linux）查到这台机器的
内网 IP，别的设备访问 `http://<内网 IP>:7860` 即可。

> ⚠️ `0.0.0.0` 意味着**同一网络里任何人都能打开这个页面、并执行命令**。
> 只在信得过的网络里这么做；要放到公网，务必自己加上认证与反向代理。

## 说明

- **默认只监听本机**（`127.0.0.1:7860`）：同一局域网里的其他设备连不上。
  这是刻意保守的默认值 —— 工作台的 `run_command` 会在宿主机执行命令。
  要开放见 [开放给局域网](#开放给局域网)。
- **放公网前必须自己加认证与反向代理**，裸奔等于把 shell 挂到网上。
  安全模型与漏洞上报方式见 [SECURITY.md](SECURITY.md)。
- **外部命令引擎会在本机直接执行你填的那条命令**，没有二次确认 ——
  只填你自己信得过的命令。
- 成员名、人设、共享记忆前缀都可以随便改；项目本身不绑定任何特定 AI。

## 许可

MIT —— 见 [LICENSE](LICENSE)。

## 参与与反馈

- 加群聊天：**QQ 交流群 874924265**（安装 / 配置 / 插件问题，或想晒自己的分身）
- 想一起改：见 [CONTRIBUTING.md](CONTRIBUTING.md)（装环境 / 跑测试 / 提 PR）
- 版本变更：见 [CHANGELOG.md](CHANGELOG.md)
- 安全问题：见 [SECURITY.md](SECURITY.md)
