---
name: nexus-mod-install
description: 当用户需要从 Nexus Mods (N网) 搜索、下载、安装、更新、打汉化、查看清单、卸载/删除或恢复游戏模组 (Mod) 时触发。覆盖已授权白名单游戏（星露谷物语 SDV、赛博朋克2077 CP2077、荒野大镖客2 RDR2、影之刃零 PBZ），支持中文功能意图语义搜索、Nexus 网页链接或 Mod ID 直装、nxm:// 协议直装、本地 zip/7z/rar 包自适应去嵌套部署、前置依赖自动联装，以及免费账号 403 自动复用桌面 Chrome 会话模拟下载。
metadata:
  short-description: 多游戏 Nexus 模组检索、四级自动下载（含浏览器热复用模拟）、多格式自适应安装与软删除恢复
---

# Nexus 模组下载、安装与管理 Skill

核心脚本目录：[scripts/](./scripts/) | 配置字典参考：[config-and-mappings.md](./references/config-and-mappings.md) | 多游戏目录规范：[game-mod-layouts.md](./references/game-mod-layouts.md)

---

## 1. 零返工执行准则与铁律约束 (必读)

1. **免读源码直跑规范（消除隐性探索开销）**：
   - 本 `SKILL.md` 已完整列出全部 CLI 参数、输出结构与拦截行为。处理常规搜索、下载、安装、汉化、清单查看、卸载恢复及白名单/离线拦截任务时，**直接运行下方对应的单条 SOP 命令即可，切勿预先 `read_file` 翻阅 `scripts/*.py`、`scripts/*.js`、`mod_config.json` 或 `mod_mappings.json` 源码**。
   - 安装脚本末尾已自动输出结构化 **`[安装验证摘要]`**（包含命中下载通道、真实版本、物理落盘路径及顶层文件列表无嵌套校验、前置依赖状态含 `ContentPackFor`、专属清单活跃总数）；`--close-browser` 亦会输出明确关窗确认。**看到 `[安装验证摘要]` 后无需再跑额外的 `Get-ChildItem` 或读取 `manifest.json` 重复复查**。
2. **单游戏隔离与白名单保护**：
   - 每次只操作用户指定的单一游戏（`-g <game>` 支持中英文及缩写：`stardewvalley`/`星露谷`/`sdv`、`cyberpunk2077`/`赛博朋克2077`/`2077`、`reddeadredemption2`/`荒野大镖客2`/`rdr2`、`phantomblandzero`/`影之刃零`/`pbz`，同时已内置巫师3、老头环、博德之门3、环世界等常见中文名归一化）。
   - **当返回 `[白名单拦截]` 时**：说明该游戏已完成中英文域名归一化且确实不在 `mod_config.json` 授权白名单中——**切勿更换英文域名重复重试**，仅在用户明确要求授权添加时才执行 `--add-game "<game>"`。
3. **外接移动硬盘离线保护（预期行为，无需排查源码）**：
   - 若游戏安装在移动硬盘（如 `E:/SteamLibrary/...`）且磁盘当前未挂载：`manage_mod.py -g <game> --list` 可正常离线读取并展示历史清单；而执行 `--uninstall` / `--reinstall` / 安装会触发 `✗ [磁盘未挂载]` 安全退出以保护专属清单不被清空或失步。遇到此提示时**无需翻查脚本源码或重试，直接向用户汇报清单并提醒先连接对应磁盘即可**。
4. **登录与人机验证合规边界**：
   - 桌面 Chrome 模拟下载提示 `[等待登录]` 或 `[等待验证]` 时，**严禁尝试通过脚本/工具绕过或点击验证码 (CAPTCHA / Cloudflare Turnstile)**；提示用户在已弹出的桌面 Chrome 窗口手动完成即可（登录态持久保存在 `./.chrome_mod_profile`）。

---

## 2. Agent 标准执行决策流 (SOP)

所有 `nexus_mod_install.py` 命令务必附加 `--non-interactive` 防止阻塞。可按需用 `;` 在同一条终端命令中串联连贯操作（例如本地安装 + 卸载 + 恢复）。

### 场景 A：按模组名或中文功能意图搜索安装（如“装个钓鱼简化 mod 并打上汉化”）
```bash
# 标准搜索安装（自动完成：中文意图转译 -> 热度排序选第1项 -> 前置依赖联装 -> 四级通道下载 -> 去双层嵌套 -> 登记清单）
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py -g <game> -q "<关键词或中文功能描述>" --select-index 1 --non-interactive

# 如用户同时要求“顺便打上中文汉化/中文配置”，追加 --with-translation（支持内置汉化包合并）
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py -g <game> -q "<关键词>" --select-index 1 --with-translation --non-interactive
```

### 场景 B：已知 Nexus 网页链接或 Mod ID 直装（跳过检索，精准直达）
```bash
# 传入 Nexus 网页链接直装（自动提取游戏域名、Mod ID 与 File ID；若用户要求装完关闭弹出的浏览器则加 --close-browser）
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py -g <game> --url "https://www.nexusmods.com/<game>/mods/<ID>" --non-interactive [--close-browser]

# 传入已知 Mod ID 直装（可选 -f <FILE_ID> 指定特定文件，可选 --close-browser 完成后关窗）
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py -g <game> --mod-id <MOD_ID> [-f <FILE_ID>] --non-interactive [--close-browser]
```

### 场景 C：本地压缩包 (`.zip` / `.7z` / `.rar`)、`nxm://` 协议链接或独立汉化包覆盖
```bash
# 本地压缩包直装（内置文件头魔数识别，原生支持 .zip / .7z / .rar 自动解压与去双层嵌套）
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py -g <game> --install-zip "<本地压缩包绝对路径>" --non-interactive

# NXM 协议授权直装
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py --nxm "nxm://<game>/mods/<ID>/files/<FID>?key=...&expires=..." --non-interactive

# 用本地独立汉化压缩包覆盖已安装模组的 i18n 目录（自动保护原有 manifest.json 与 config.json）
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py -g <game> --i18n-apply "<汉化包路径>" --target-mod "<目标模组文件夹名>" --non-interactive
```

### 场景 D：单游戏清单查看、软删除卸载与一键恢复（首选轻量级 `manage_mod.py`）
```bash
# 查看指定游戏的活跃模组清单 / 已卸载归档记录（纯读取 JSON，支持外接硬盘离线状态秒开）
python .agents/skills/nexus-mod-install/scripts/manage_mod.py -g <game> --list
python .agents/skills/nexus-mod-install/scripts/manage_mod.py -g <game> --list-uninstalled

# 软删除卸载模组（支持英文名、文件夹名、UniqueID 或中文译名如 "自动化"，移入 deleted_mods/<game>/ 归档并保留 Nexus ID）
python .agents/skills/nexus-mod-install/scripts/manage_mod.py -g <game> --uninstall "<模组名或中文名>"

# 从归档备份一键还原已卸载模组
python .agents/skills/nexus-mod-install/scripts/manage_mod.py -g <game> --reinstall "<模组名或中文名>"
```

### 场景 E：物理目录重扫校准、模组更新检查与授权新游戏
```bash
# 重新扫描单游戏物理目录并刷新中文名与专属清单 installed_mods/<game>.json
python .agents/skills/nexus-mod-install/scripts/scan_single_game.py -g <game>

# 检查单游戏已关联 Nexus ID 的模组更新并自动升级
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py -g <game> --check-updates --non-interactive [--close-browser]

# 仅在用户明确授权添加新游戏时执行：按需探测并写入 mod_config.json 白名单
python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py --add-game "<游戏名>" [--game-path "<自定义游戏根目录>"]
```

---

## 3. 核心参数速查表 (`nexus_mod_install.py`)

| 参数 | 说明 |
| :--- | :--- |
| `-g, --game <name>` | 目标游戏域名或中英文别名（缺省使用 `mod_config.json` 的 `defaultGame`）。 |
| `-q, --query <text>` | 搜索词（支持中文功能词自动转译英文，或直接传入 Nexus 链接）。 |
| `--select-index <N>` | 搜索结果选取序号（`1` 代表下载量最高的第 1 名正统模组）。 |
| `-id, --mod-id <int>` | 直接按 Nexus Mod ID 下载安装（配合可选 `-f, --file-id <int>`）。 |
| `--url <url>` | 支持 Nexus 模组网页链接、GitHub Release 直链或 `nxm://` 链接。 |
| `--install-zip <path>` | 安装本地 `.zip` / `.7z` / `.rar` 包（支持可选 `--mod-id`、`--mod-name`、`--mod-version`）。 |
| `--with-translation` | 安装模组时自动生成/合并内置中文 `i18n` 语言文件。 |
| `--i18n-apply <path>` | 配合 `--target-mod <folder>` 将独立汉化包合并进目标模组 `i18n/`。 |
| `--close-browser` | 触发桌面 Chrome 模拟下载后自动关闭专用 Chrome 窗口（不传则默认保留热会话加速连续下载）。 |
| `--no-simulate-browser` | 禁用免费账号 403 时的桌面 Chrome 自动模拟回退（仅试探 CDN / 开源镜像 / 本地包）。 |
| `--no-download-deps` | 仅提示缺失的 Nexus 前置模组而不自动联装（默认开启自动联装）。 |
| `-c, --config <path>` | 指定自定义 `mod_config.json` 路径（亦可通过环境变量 `MOD_CONFIG_PATH` 全局指定）。 |
| `--non-interactive` | 非交互模式，Agent 自动化调用时必传。 |

---

## 4. 四级自动下载通道与结果汇报要点

安装时内部按 **Tier 1 官方 CDN (`nexus_cdn`) → Tier 2 GitHub 最新 Release 开源镜像 (`upstream_mirror`) → Tier 3 本地下载目录接管 (`local_pickup`) → Tier 4 桌面 Chrome 原生 CDP 热会话模拟点击 Slow Download (`browser_cdp`)** 自动回退。

执行完毕后，直接依据命令输出的 **`[安装验证摘要]`** 向用户简洁汇报：
1. **模组与通道**：名称（含中文译名）、版本号、Nexus ID 与实际命中的下载通道；
2. **部署路径与结构**：确认目标路径及顶层文件无双层嵌套（若有 `--close-browser` 说明浏览器已关闭）；
3. **前置依赖与清单**：说明前置依赖就绪情况（或需额外关注的内容包前置如 `Content Patcher`），以及专属清单当前活跃模组数。
