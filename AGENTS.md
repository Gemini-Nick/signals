# Project Rules

- 本项目拥有 Signals 的数据同步、信号计算、三池、回测、API、MCP、报告渲染和 Signals 自动化。
- 纯市场解释、个股研究和报告阅读进入 `市场研究｜A股与 WorkBuddy`。
- WorkBuddy Replay 脚本、skill、rubric、manifest 和安装流程进入 `WorkBuddy｜复盘工程`。
- Electron、运行时、安装包和第二屏 UI 进入 `隆小侠｜Agent OS`。
- 自动化输出首行 `DONT_NOTIFY` 时立即停止，不调用 MCP、不读取账号、不发送微信。
- 未经明确授权不发送微信、不修改生产开关、不触发大范围数据生产。
- 测试使用 `.venv/bin/pytest`；大型定向测试前先用 `rg --files tests` 确认路径。
- 保留用户未提交修改；不要用破坏性 Git 命令清理工作树。
