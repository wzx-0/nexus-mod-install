# 配置文件与字典映射参考 (`mod_config.json` & `mod_mappings.json`)

本文件详细说明 `nexus-mod-install` Skill 的跨机器便携环境配置、热更新规则映射表与 MCP 配置结构。日常执行安装无需读取本文件，仅在**初始化新机器环境、排障路径问题、新增翻译映射或扩展开源镜像源**时查阅。

---

## 1. 运行环境与白名单配置 (`mod_config.json`)

位于项目根目录 `./mod_config.json`，集中管理 API Key、环境路径与已授权游戏白名单：

```json
{
  "defaultGame": "stardewvalley",
  "nexusApiKey": "<YOUR_NEXUS_API_KEY>",
  "environment": {
    "chromePath": "auto",
    "chromeProfileDir": "./.chrome_mod_profile",
    "cdpPort": 9222,
    "downloadsDir": "auto",
    "extraDownloadDirs": [],
    "tmpDownloadsDir": "./tmp_downloads",
    "backupDir": "./deleted_mods",
    "installedModsDir": "./installed_mods",
    "mappingsFile": "./mod_mappings.json"
  },
  "games": {
    "stardewvalley": {
      "name": "Stardew Valley",
      "gamePath": "C:/Program Files (x86)/Steam/steamapps/common/Stardew Valley",
      "modsPath": "C:/Program Files (x86)/Steam/steamapps/common/Stardew Valley/Mods",
      "gameDomain": "stardewvalley",
      "status": "installed_active",
      "installed_mods_file": "./installed_mods/stardewvalley.json"
    }
  }
}
```

### 关键字段说明
- `chromePath`: `"auto"` 自动探测 Chrome / Edge 安装路径，也可填绝对路径。
- `chromeProfileDir`: 专用浏览器登录态持久化目录（默认 `./.chrome_mod_profile`）。用户只需在此窗口登录一次 Nexus Mods，后续自动模拟下载永久复用 Cookie。
- `cdpPort`: 本地 Chrome DevTools 调试端口（默认 `9222`），脚本优先复用已监听该端口的浏览器会话。
- `extraDownloadDirs`: 额外监控的下载目录（`config_manager.py --init` 会自动从 Chrome `Preferences` 读取 `download.default_directory` 写入此处）。
- `games`: **已授权游戏白名单**。未经 `--add-game` 显式授权，任何脚本不得擅自向此节点写入新游戏。

### 环境初始化与路径排障命令
```bash
# 同步环境目录、自动探测浏览器下载目录、生成 .mcp.json / mcp_config.json
python .agents/skills/nexus-mod-install/scripts/config_manager.py --init

# 查看当前动态解析后的全部绝对路径
python .agents/skills/nexus-mod-install/scripts/config_manager.py --show-paths

# 首次在新电脑使用模拟下载前，打开专用 Profile 登录向导保存会话
python .agents/skills/nexus-mod-install/scripts/chrome_simulate_download.py --login
```

---

## 2. 热更新字典与镜像配置 (`mod_mappings.json`)

位于项目根目录 `./mod_mappings.json`，具备基于文件 `mtime` 的热加载能力，直接修改 JSON 即刻生效，无需改动 Python/JS 源码：

| 顶层节点 | 作用 |
| :--- | :--- |
| `game_profiles` | 各游戏的 `mods_subdir`、特征文件 `signature_files`、默认安装策略 `install_mode` 与 `nexus_game_id`。 |
| `translations` | 各游戏模组中英文对照名 (`mod_names`)、星露谷 NPC 译名 (`npc_names`)、功能词 (`keywords`) 及影之刃零预置元数据 (`mod_metadata`)。 |
| `search_keywords` | 用户中文自然语言意图（如 `"钓鱼简化"`、`"衣柜"`、`"地图追踪"`）到 Nexus 英文精确检索词的映射表。 |
| `upstream_mirrors` | 官方开源模组（SMAPI、CET、RED4ext、ArchiveXL、TweakXL、redscript、UI Info Suite 2 等）的 `github_repo` 与备用直链，支持通过 GitHub Releases API 动态获取最新版本资产。 |
| `ue4ss` | UE4SS 框架内置模块目录过滤表 (`builtin_folders`)，避免将内置脚本误统计为独立用户模组。 |

### 新增开源镜像示例
在 `mod_mappings.json` 的 `upstream_mirrors` 节点下追加条目即可让免费账号享受秒级直连下载：
```json
"my_open_source_mod": {
  "name": "Display Name",
  "game_domain": "stardewvalley",
  "mod_id": 12345,
  "version": "latest",
  "github_repo": "owner/repo",
  "asset_keyword": "",
  "asset_exclude": ["debug", "source"],
  "url": "https://github.com/owner/repo/releases/download/v1.0.0/Mod.zip",
  "filename": "Mod.zip"
}
```
