# nexus-mod-install

> **Nexus Mods 跨游戏智能自动化模组安装与管理 Agent Skill & MCP 服务**

`nexus-mod-install` 是专为 Antigravity / AI Agent 设计的游戏模组（Mod）自动化下载、安装、汉化、依赖处理与卸载管理工具集。

---

## 🌟 核心特性

- **🛡️ 游戏白名单安全隔离**
  - 严格限制在白名单游戏范围内：**星露谷物语 (Stardew Valley)**、**赛博朋克2077 (Cyberpunk 2077)**、**荒野大镖客2 (Red Dead Redemption 2)**、**影之刃零 (Phantom Blade Zero)**。
  - 严禁擅自扫描非白名单游戏目录，杜绝越界误操作。

- **🔍 中文功能意图与语义检索**
  - 内置 `mod_mappings.json` 规则映射库，支持常见别名中英对照（如“拖拉机”、“SVE”、“季节外观”等）。
  - 自动识别汉化包与对应前置依赖，优先提供 GitHub 加速镜像（如 SMAPI、Content Patcher、Vortex 等）。

- **🌐 全格式输入支持**
  - 支持 **Nexus Mods 网页直链**（`nexusmods.com/<game>/mods/<id>`）。
  - 支持 **Mod ID**（如 `stardewvalley:2400`）。
  - 支持 **`nxm://` 协议链接**。
  - 支持 **本地压缩包文件**（`.zip`、`.7z`、`.rar`、`.tar.gz`）。

- **⚡ 免费账号 403 自动复用 Chrome CDP 会话模拟下载**
  - 针对 Nexus Mods 免费用户的 API 限制与 403 权限拦截，无缝调用 Chrome DevTools MCP 及专用浏览器实例。
  - 自动感知页面跳转、点击 `Slow Download` 并监听捕获浏览器落盘文件，无需付费会员。
  - 浏览器会话 Cookie 隔离持久化于本地专用目录，一次登录永久复用。

- **📦 智能自适应去嵌套部署 (Adaptive Unnesting)**
  - 自动识别解压层级结构（单模组、多模组嵌套合集、打包前缀）。
  - 精准对齐到各游戏的标准目录（如星露谷的 `Mods/`、大镖客2的 `lml/`、2077的 `archive/pc/mod/`）。

- **♻️ 软删除备份与清单保护**
  - 卸载模组时采用软删除机制（自动归档至 `deleted_mods/`），支持随时一键恢复。
  - 离线清单（`installed_mods/`）结构化存储，支持单游戏独立清单与跨游戏统一管理。

---

## 📂 项目结构

```text
.
├── .agents/
│   └── skills/
│       └── nexus-mod-install/
│           ├── SKILL.md                 # Agent Skill 核心标准操作规程 (SOP)
│           ├── agents/
│           │   └── openai.yaml          # Agent UI 配置与交互元数据
│           ├── references/
│           │   ├── config-and-mappings.md  # 配置规范与映射字典文档
│           │   └── game-mod-layouts.md     # 游戏安装目录与模组结构规范
│           └── scripts/
│               ├── nexus_mod_install.py       # 模组搜索、解析与调度核心引擎
│               ├── adaptive_installer.py      # 自适应去嵌套解压与部署引擎
│               ├── config_manager.py          # 环境路径解析与 MCP 配置管理
│               ├── game_detector.py           # 本机 Steam 游戏与路径自动探测器
│               ├── chrome_simulate_download.py # Chrome CDP 模拟网页下载引擎
│               ├── mcp_nexus_download.js      # Chrome DevTools MCP 下载脚本
│               ├── manage_mod.py              # 模组清单查看、软删与恢复管理
│               ├── scan_single_game.py        # 单游戏离线模组扫描工具
│               └── launch_on_desktop.py       # 桌面会话拉起辅助工具
├── .mcp.json                            # Antigravity Chrome DevTools MCP 服务配置
├── mcp_config.json                      # 备用 MCP 配置文件
├── mod_config.example.json              # 配置文件模板（包含占位符与默认路径）
├── mod_mappings.json                    # 中文映射、别名与依赖镜像字典
├── .gitignore                           # 严格排除敏感 Key、Cookie、本地模组与备份
└── README.md
```

---

## 🚀 快速开始

### 1. 环境准备

- **Python**: 3.10+
- **Node.js**: 需预装并支持 `npx`
- **Google Chrome**: 需安装桌面版 Chrome
- 安装 Python 依赖：
  ```bash
  pip install requests
  ```

### 2. 初始化项目配置

复制配置模板：
```bash
cp mod_config.example.json mod_config.json
```

编辑 `mod_config.json`：
- 可选填入你的 Nexus Mods API Key（登录 Nexus Mods 后在个人设置中生成）：
  ```json
  "nexusApiKey": "<YOUR_NEXUS_API_KEY>"
  ```
- 运行探测脚本自动识别本机安装的游戏与目录：
  ```bash
  python .agents/skills/nexus-mod-install/scripts/game_detector.py
  ```

### 3. 一次性登录 Nexus Mods（适用于免费用户模拟下载）

如果使用 Chrome 模拟下载，只需运行一次登录流程：
```bash
python .agents/skills/nexus-mod-install/scripts/chrome_simulate_download.py --login
```
在弹出的专用浏览器窗口中登录你的 Nexus Mods 账号后关闭窗口即可。登录态 Cookie 将安全存储在本地 `.chrome_mod_profile/` 目录中。

---

## 🛠️ 常用命令与使用示例

### 1. 检索模组
```bash
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py search "拖拉机" --game stardewvalley
```

### 2. 通过 URL 直装模组
```bash
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py install "https://www.nexusmods.com/stardewvalley/mods/1401"
```

### 3. 本地压缩包去嵌套安装
```bash
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py install "C:/Downloads/SVE-1.15.zip" --game stardewvalley
```

### 4. 模组清单与管理
```bash
# 查看星露谷已安装模组
python .agents/skills/nexus-mod-install/scripts/manage_mod.py list --game stardewvalley

# 软删除模组（自动备份）
python .agents/skills/nexus-mod-install/scripts/manage_mod.py remove "TractorMod" --game stardewvalley

# 恢复已软删除模组
python .agents/skills/nexus-mod-install/scripts/manage_mod.py restore "TractorMod" --game stardewvalley
```

---

## 🔒 隐私与安全声明

本项目严格遵循本地安全与隐私隔离规范：
- 本地 `mod_config.json`（含私人 API Key）、`.chrome_mod_profile/`（含 Chrome 登录 Cookie）、`deleted_mods/`、`installed_mods/` 与下载缓存均已包含在 `.gitignore` 中。
- 不会将任何私人凭据、个人浏览器记录或本地游戏存档上传至远程代码仓库。
