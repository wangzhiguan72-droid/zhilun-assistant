# AGENTS.md

**所有并行智能体（Codex / WorkBuddy / Claude Code / ZCode 等）请先阅读
[`CLAUDE.md`](./CLAUDE.md)** —— 它是本项目并行协作约定的唯一真源：
目录归属、单一真源清单、测试与 CI 契约、提交与推送纪律、平台踩坑笔记都在那里。

一行摘要：改 `desktop.spec` 记得补 datas；版本号只改 `version.py`；
测试命名 `*_test.py`；前端契约变更要补 `_syntaxcheck/*_probe.js`；
全量回归跑 `scripts/regress_run.py`；**推送权在用户，任何会话不得 push**。
