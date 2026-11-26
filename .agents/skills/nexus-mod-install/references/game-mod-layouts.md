# 多游戏模组目录结构、自适应解压规则与排障指南

本文件记录白名单内四款核心游戏的模组物理布局、压缩包自适应处理逻辑及常见排障方案。

---

## 1. 四款白名单游戏模组部署规范

| 游戏名称 | 配置 Domain (`-g`) | Nexus 域名 | 游戏根目录子结构与分流策略 |
| :--- | :--- | :--- | :--- |
| **星露谷物语** (Stardew Valley) | `stardewvalley` (别名: `星露谷`, `sdv`) | `stardewvalley` | 统一部署至 `<gamePath>/Mods/<ModFolder>/manifest.json`。若压缩包存在多层外壳（如 `Outer-1234/ModA/manifest.json`），自动剥离外层包裹目录，杜绝双层嵌套导致 SMAPI 无法识别；若单包内含多个子模组（如 `[CP]` + `[FTM]`），自动并列展开至 `Mods/`。 |
| **赛博朋克 2077** (Cyberpunk 2077) | `cyberpunk2077` (别名: `2077`, `cp2077`) | `cyberpunk2077` | 支持复合目录合并 (`merge_game_root`)：<br>• 若压缩包内含 `archive/`、`bin/`、`r6/`、`red4ext/`、`mods/`（含单层外包目录），自动对齐合并至 `<gamePath>` 并在清单记录 `installed_files` 文件明细；<br>• 纯 `.archive`/`.xl` 自动放入 `archive/pc/mod`；<br>• 纯 `.reds` 自动放入 `r6/scripts/<ModName>`；<br>• 含 `init.lua` 自动放入 `bin/x64/plugins/cyber_engine_tweaks/mods/<ModName>`。 |
| **荒野大镖客 2** (Red Dead Redemption 2) | `reddeadredemption2` (别名: `荒野大镖客2`, `rdr2`) | `reddeadredemption2` | 双路自动分流：<br>• 含 `install.xml` 或 `lml/` 结构的模组部署至 `<gamePath>/lml/<ModName>`；<br>• 含 `.asi` 的 ScriptHook 插件自动提取部署至游戏根目录 `<gamePath>` 并记录文件明细。 |
| **影之刃零** (Phantom Blade Zero) | `phantomblandzero` (别名: `影之刃零`, `pbz`) | `phantombladezero` | 双引擎自动分流：<br>• **PAK 资源模组**（`.pak`/`.ucas`/`.utoc`）提取至 `<gamePath>/ProjectPBZ/Content/Paks/~mods`；<br>• **UE4SS 脚本/DLL 模组**（含 `Scripts/main.lua` 或 `dlls/main.dll`）部署至 `<gamePath>/platform/steam/ue4ss/Mods/<ModName>`，去双层嵌套并自动补全 `enabled.txt`。 |

---

## 2. 压缩包格式与魔数校验机制

Nexus Mods 上的模组常混用 `.zip`、`.7z` 与 `.rar`。`adaptive_installer.py` 采用**文件头魔数 (Magic Bytes) 识别**而非盲信文件后缀：
- `PK\x03\x04` → `zipfile` 原生解压
- `7z\xbc\xaf\x27\x1c` → `py7zr` 优先，遇到特殊压缩算法自动回退 `7z.exe` 或系统内置 `C:\Windows\System32\tar.exe`
- `Rar!\x1a\x07` → 自动调用 `7z.exe` 或 Windows 10/11 内置 `tar.exe` (`libarchive`) 解压
- `<!DOCTYPE` / `<html` → 立即报错拦截，防止将未授权重定向网页当作压缩包写入游戏目录

---

## 3. 复合模组安全卸载与还原机制

- **独立目录模组**（如星露谷、UE4SS 子目录）：软卸载时整目录移入 `./deleted_mods/<game_domain>/<mod_key>`。
- **跨目录复合模组**（如 2077 跨 `archive` + `r6` + `bin` 模组、RDR2 根目录 `.asi`）：安装时在 `installed_mods/<game_domain>.json` 中记录 `installed_files` 相对路径数组；卸载时按数组精确逐文件移入归档目录，**内置根目录保护锁，绝不误移共享父目录**；执行 `--reinstall` 时按原相对路径逐一还原。

---

## 4. 常见问题排障

1. **桌面浏览器弹出 Nexus 登录页或安全验证页**：
   - 保持窗口在前台，提示用户手动完成登录或点击验证（**严禁通过脚本或任何自动化手段尝试破解/绕过人机验证码**）。
   - 登录完成后脚本会自动感知页面跳转、自动点击 `Slow Download` 并捕获落盘文件；登录 Cookie 会保存在 `./.chrome_mod_profile` 中，后续运行无需再次登录。
2. **连续下载多个模组（或前置依赖 + 主模组）**：
   - `mcp_nexus_download.js` 与 `chrome_simulate_download.py` 默认开启**会话热复用**，首个模组下载完成后保留 Chrome 端口 `9222` 就绪，后续模组直接在现有标签页导航点击，省去浏览器冷启动开销；如需全部装完后关闭窗口，可在最后一条命令附加 `--close-browser`。
3. **外接移动硬盘/分盘游戏离线保护**：
   - 当游戏安装在移动硬盘（如 `E:/SteamLibrary/...`）且磁盘当前未连接时，`--list` 与 `--list-uninstalled` 仍可正常离线查阅清单；而扫描、安装、卸载或恢复操作会自动触发 `[离线保护]` / `[磁盘未挂载]` 拦截，**绝不清空已有的 `installed_mods/<game>.json` 记录，也不在未挂载盘符下误改状态**。
