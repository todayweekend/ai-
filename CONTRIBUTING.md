# 参与开发

欢迎提 issue 和 PR。

这个项目刻意保持**零构建**：后端就是一个 `app.py`，前端就是一个
`static/index.html`，没有打包步骤、没有前端框架、没有 npm。

## 先跑起来

```bash
python -m venv venv

# Windows
venv\Scripts\pip install -r requirements.txt
# macOS / Linux
venv/bin/pip install -r requirements.txt

python app.py          # Windows 上也可以直接双击 start.bat
```

打开 <http://localhost:7860>。第一次启动会生成一份**空配置**，进
「设置 → AI 成员与模型 → ＋ 添加成员」填上接口地址和 Key 就能开聊。

## 跑测试

```bash
venv\Scripts\python.exe tests\test_plugins.py     # 插件系统端到端
venv\Scripts\python.exe tests\test_engine.py      # 工作引擎与身份文件
```

两个脚本都会真起一次服务、真发一次请求，不是 mock。

> 工作台里**还没有成员**时，`test_plugins.py` 会自动跳过「发一条真实消息」
> 那组用例 —— 所以别人拿到一份空工作台也能跑通，不会红一片。

## 提 PR 前请自查

- [ ] 两个测试都通过
- [ ] 没有提交 `config.json` / `settings.json` / 聊天记录 / 人设文件（`.gitignore` 已挡）
- [ ] 新代码里没有**你会后悔公开**的字符串：本机用户名、绝对路径、真实 API Key、
      内部项目名（这个坑踩过，所以写在这儿）
- [ ] 改了前端就 `Ctrl+F5` 强刷一次再确认，而不是只看代码
- [ ] 改了 `.bat` / `.vbs` 就用十六进制编辑器或 `file` 确认一下 **仍是 CRLF + ANSI 编码**

## 代码约定

- **标准库优先**。硬依赖只有 `fastapi` 和 `uvicorn`；要加新依赖请在 PR 里说明理由。
- **不加构建步骤、不引前端框架**。这是刻意的设计，不是还没做。
- 注释和文档写中文（主要面向中文用户）；标识符用英文。
- 修改核心逻辑时，顺手在 `CHANGELOG.md` 记一条。

## 加一个插件（不用改核心代码）

在 `plugins/` 下新建一个目录，放一个 `plugin.json` 和一个 `main.py` 就行。
完整钩子清单见 [`plugins/README.md`](plugins/README.md)；
可照着抄的现成例子在 `plugins/example-skin/`（会改界面和回复文案）和
`plugins/example-stats/`（最简：注册钩子 + 自己的接口）。

## 报告 bug

用仓库里的 issue 模板就行，记得把**控制台里整段报错**贴上来 —— 只贴最后一行
通常看不出原因。
