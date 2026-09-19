# 星轨夜空皮肤（example-skin）

一个把「插件能改什么」全演示一遍的样板插件。**默认停用** —— 在
「设置 → 工作台插件」里点「启用」+「⟳ 重载插件 + 界面」就能看到效果。

| 它改了工作台的… | 靠什么实现 |
|---|---|
| 背景板（星空图） | `plugin.json` → `ui.background.image = bg.svg` |
| 配色 / 圆角 / 强调色 | `ui.css` 覆盖 `--bg --panel --accent --r-msg …` |
| 左侧功能栏多了个 🌌 图标 | `ui.js` → `WB.addRailButton(...)` |
| 设置面板多了一行勾选 | `plugin.json` → `ui.snippets.settings` + `snippets/settings.html` |
| 每条回复末尾多了「✦ 星轨」 | `main.py` → `before_send` 钩子 |
| 会话里可以发 `/skin` | `main.py` → `command` 钩子 |
| 自己的面板页 + 状态接口 | `panel.html` + `api.route("GET", "/api/example-skin/stats")` |
| 重启不丢的统计 | `api.store`（存在本目录 `.data.json`） |

## 怎么改造成自己的

1. 复制整个文件夹，改名（比如 `my-skin`）；
2. 改 `plugin.json` 里的 `name`（必须跟文件夹名一致）、`title`、`description`；
3. 换掉 `bg.svg`（或指向别的图）、调 `ui.css` 的变量、改 `ui.js` 的按钮；
4. 设置 → 工作台插件 → 「⟳ 重载插件 + 界面」。

完整钩子清单与 `window.WB` 接口见「设置 → 工作台插件 → 开发指南」，或上级目录的 `README.md`。
