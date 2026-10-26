# Agent 代码已迁移

2026-10-26 起，Agent 实现集中在根目录 [agent/](../../agent/README.md)。

- 业务架构与阅读顺序：[agent/README.md](../../agent/README.md)
- 配置、接口、操作与验证：[agent/USAGE.md](../../agent/USAGE.md)

本目录仅保留旧在线验收命令的兼容入口；原运行器、模型、工具、证据校验和反馈实现均已迁走，没有第二套实现。新命令为 `python -m agent.evaluation.smoke --scenario all`。
