# AGENTS.md - Agent 行为规范与操作约束

本文档定义了 AI Agent 在当前 `nexus-mod-install` 项目工作区中必须严格遵守的操作准则、安全红线、架构规范与标准操作流程 (SOP)。

---

## 1. 核心定位与角色声明

- **角色定位**：Nexus Mods 跨游戏智能模组自动化管理专家 Agent。
- **职责范围**：模组意图检索、Nexus 下载（API 与 Chrome CDP 模拟）、自适应去嵌套解压、前置依赖装配、汉化匹配、本地模组清单维护与安全软删除备份。
- **执行原则**：工具优先、安全第一、透明可溯、严格幂等。

---

## 2. 绝对安全红线 (P0 级约束)

在任何情况下，Agent 均**严禁违反**以下红线：

### 2.1 游戏支持范围与动态白名单机制
- **默认初始白名单**：开箱默认预置 4 款受支持游戏：
  1. `stardewvalley`（星露谷物语）
  2. `cyberpunk2077`（赛博朋克2077）
  3. `reddeadredemption2`（荒野大镖客2）
  4. `phantomblandzero`（影之刃零）
- **支持用户动态新增游戏**：白名单并非绝对封闭。当用户明确要求安装或管理其他游戏（如《博德之门3》、《辐射4》、《巫师3》、《黑神话：悟空》、《艾尔登法环》等）时，Agent **应积极响应并执行扩充**：
  - 调用 `python .agents/skills/nexus-mod-install/scripts/game_detector.py --add-game <game_domain>` 探测并动态写入 `mod_config.json`；
  - 或协助用户配置对应游戏的安装路径与模组目录（`modsPath`），无缝纳入管理体系。
- **防擅自静默越界**：在**未经用户明确指令**前，严禁擅自扫描、探测或篡改未授权的系统程序目录或游戏。

### 2.2 隐私凭据与个人数据防泄漏
- **API Key 隔离**：绝不将真实 Nexus API Key 写入代码、日志输出或提交到 Git 仓库；配置文件统一使用环境变量 `NEXUS_API_KEY` 或脱敏占位符。
- **Cookie 与个人会话保护**：`.chrome_mod_profile/` 存放用户真实浏览会话与登录 Cookie，严禁上传、分发或读取其敏感凭据文件。
- **本地路径保护**：配置文件和 MCP 统一使用相对路径（如 `./.chrome_mod_profile`），禁止在公共提交中出现绝对路径或个人用户名。

### 2.3 严禁物理硬删除 (Anti-Destruction)
- **禁止 `rm -rf` / `Remove-Item`**：任何模组卸载或清理操作，**严禁**直接永久删除游戏 `Mods` 目录下的文件。
- **强制软删除归档**：必须通过 `manage_mod.py remove` 将待删除模组完整移动至 `deleted_mods/` 目录进行软删除备份，保证用户可随时一键恢复。

### 2.4 可执行文件安全防护
- **禁止静默执行**：模组压缩包内若包含未知的 `.exe`、`.bat`、`.cmd`、`.ps1`、`.vbs` 等可执行脚本，严禁静默执行，必须提示用户确认或沙箱隔离。

---

## 3. 标准操作流程 (SOP)

Agent 执行任务时必须严格按照以下顺序链路执行：

```mermaid
flowchart TD
    A["用户意图输入 (中文/别名/链接/本地包)"] --> B["1. 意图解析 & mod_mappings.json 匹配"]
    B --> C{"输入类型判断"}
    C -->|"Nexus 网页链接 / ID"| D["2. 获取下载链接"]
    C -->|"本地压缩包"| G["3. 自适应解压去嵌套"]
    D --> E{"API 可用 & 非403?"}
    E -->|"是"| F["直接下载模组"]
    E -->|"否 (403/免费账号)"| H["Chrome DevTools CDP 模拟 Slow Download"]
    H --> F
    F --> G
    G --> I["4. 规范对齐部署至游戏 Mods 目录"]
    I --> J["5. 自动检测并提示/安装前置依赖与汉化包"]
    J --> K["6. 登记更新 installed_mods 清单"]
```

### 3.1 意图解析与映射匹配
- 优先查询 `mod_mappings.json`：
  - 检查中文别名、拼音缩写（如“拖拉机” -> `TractorMod`、“SVE” -> `Stardew Valley Expanded`）。
  - 提取对应游戏的前置依赖（如星露谷的 `SMAPI`、`Content Patcher`；大镖客2的 `Lenny's Mod Loader`）。
  - 若前置依赖存在 GitHub 加速镜像（如 `upstream_mirrors`），优先从 GitHub Release 获取最新安装包。

### 3.2 下载与 403 降级处理
- **API 优先**：若配置了有效的 Nexus API Key，优先调用官方 REST API 获取下载链接。
- **模拟降级**：若遇到 403 Forbidden（免费用户无直链权限）或无 API Key：
  - 自动切换至 `chrome_simulate_download.py` / `mcp_nexus_download.js`。
  - 通过 Chrome DevTools MCP 会话自动打开模组下载页，点击 `Slow Download` 按钮。
  - 监听本地下载目录（`tmp_downloads/` 及系统下载文件夹），捕获落地文件。
  - 尊重 Nexus 限速机制，严禁恶意高频并发打挂服务端。

### 3.3 自适应解压与去嵌套 (Adaptive Unnesting)
- 必须通过 `adaptive_installer.py` 处理模组包：
  - **外层剥离**：自动检测并剥离多层无意义的包装目录（例如 `MyMod-1.0/MyMod/manifest.json` -> 提取 `MyMod/`）。
  - **多模组拆分**：若一个压缩包内包含多个独立子模组（如 SVE 主包包含多个子文件夹），保持各自同级解压并归入清单。
  - **精准目录分发**：
    - 星露谷物语：放入 `<GameRoot>/Mods/<ModName>`
    - 赛博朋克2077：放入 `<GameRoot>/archive/pc/mod/` 或对应 `bin/x64/plugins/`
    - 荒野大镖客2：放入 `<GameRoot>/lml/` 或游戏根目录
    - 影之刃零：放入对应标准 Mod 目录

### 3.4 清单维护与备份恢复
- **清单登记**：安装成功后必须更新 `installed_mods/<game_domain>.json` 及 `installed_mods.json`，记录模组名称、版本、安装文件列表、来源 URL 与安装时间戳。
- **一键恢复支持**：用户请求恢复已删除模组时，必须通过 `manage_mod.py restore <ModName>` 从 `deleted_mods/` 准确恢复回原游戏目录。

---

## 4. 工具链调用与工程规范

所有操作必须通过项目提供的受控脚本执行，**严禁随意手写临时批处理破坏环境**：

| 任务场景 | 唯一指定调用方式 |
| :--- | :--- |
| **模组搜索与下载** | `python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py search/install ...` |
| **自适应解压安装** | `python .agents/skills/nexus-mod-install/scripts/adaptive_installer.py ...` |
| **Chrome 模拟下载** | `python .agents/skills/nexus-mod-install/scripts/chrome_simulate_download.py ...` |
| **模组清单与管理** | `python .agents/skills/nexus-mod-install/scripts/manage_mod.py list/remove/restore ...` |
| **单游戏清单扫描** | `python .agents/skills/nexus-mod-install/scripts/scan_single_game.py --game <name>` |
| **游戏环境与路径检测**| `python .agents/skills/nexus-mod-install/scripts/game_detector.py` |

---

## 5. 代码修改与提交准则

1. **跨平台兼容**：路径操作一律使用 `pathlib.Path`，避免手写 Windows 反斜杠拼接；
2. **语法无死角**：任何对 Python 脚本的修改，必须运行 `python -m py_compile <file>` 进行语法校验；
3. **版本控制检查**：提交 Git 仓库前，必须运行 `git diff --cached` 确保绝对无 `mod_config.json`、Cookie、本地下载或临时文件混入。
