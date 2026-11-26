# GEMINI.md - Project Agent Guidelines

本文件为 Antigravity / Gemini Agent 专用约束入口，完整规则继承并执行 [AGENTS.md](./AGENTS.md)。

---

## 关键指令摘要 (Quick Reference)

1. **游戏白名单**：仅限 `stardewvalley`、`cyberpunk2077`、`reddeadredemption2`、`phantomblandzero`；严禁触碰白名单外任何游戏。
2. **数据安全**：严禁提交 `mod_config.json` 与 `.chrome_mod_profile`，一律使用相对路径与脱敏模板。
3. **软删除铁律**：卸载模组必须调用 `manage_mod.py remove` 移入 `deleted_mods/`，绝对禁止物理直删。
4. **403 自动降级**：Nexus API 拦截 403 时，自动调用 Chrome DevTools MCP 模拟慢速下载。
5. **去嵌套部署**：解压必须通过 `adaptive_installer.py` 对齐游戏 Mods 目录，并更新 `installed_mods/` 结构化清单。

> 详细规范与标准操作规程 (SOP) 见 [AGENTS.md](./AGENTS.md)。
