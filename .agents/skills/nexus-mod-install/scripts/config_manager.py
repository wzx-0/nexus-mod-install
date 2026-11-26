#!/usr/bin/env python3
"""
config_manager.py — 集中化配置与路径管理器

负责统一加载、初始化与解析项目根目录的 mod_config.json、mod_mappings.json、
.mcp.json 与 mcp_config.json。
消除各脚本中分散写死的绝对路径（Chrome 路径、端口、下载目录、备份目录、Profile 目录等），
使整套系统可以在不同电脑、不同操作系统与不同安装盘符下即插即用，无需修改脚本代码。
"""

import os
import sys
import json
import shutil
import argparse
from pathlib import Path
from typing import Optional, Dict, Any, List, Set


SCRIPT_DIR = Path(__file__).resolve().parent


def find_project_root() -> Path:
    """自动定位项目根目录 (含 mod_config.json 或 .agents 或 installed_mods)"""
    candidate = SCRIPT_DIR
    for _ in range(5):
        if (candidate / "mod_config.json").exists() or (candidate / ".agents").exists() or (candidate / "installed_mods").exists():
            return candidate
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    return Path.cwd()


PROJECT_ROOT = find_project_root()
PROJECT_CONFIG = PROJECT_ROOT / "mod_config.json"
MAPPINGS_FILE = PROJECT_ROOT / "mod_mappings.json"
APPDATA_CONFIG = Path(os.environ.get("APPDATA", "")) / "NexusModManager" / "config.json"

GAME_ALIAS_MAP = {
    "星露谷": "stardewvalley", "星露谷物语": "stardewvalley", "stardew": "stardewvalley", "sdv": "stardewvalley", "stardewvalley": "stardewvalley",
    "赛博朋克": "cyberpunk2077", "赛博朋克2077": "cyberpunk2077", "2077": "cyberpunk2077", "cp2077": "cyberpunk2077", "cyberpunk": "cyberpunk2077", "cyberpunk2077": "cyberpunk2077",
    "大镖客": "reddeadredemption2", "大镖客2": "reddeadredemption2", "荒野大镖客": "reddeadredemption2", "荒野大镖客2": "reddeadredemption2", "rdr2": "reddeadredemption2", "reddeadredemption2": "reddeadredemption2",
    "影之刃零": "phantomblandzero", "影之刃0": "phantomblandzero", "影之刃": "phantomblandzero", "pbz": "phantomblandzero", "phantombladezero": "phantomblandzero", "phantom blade zero": "phantomblandzero", "phantomblandzero": "phantomblandzero",
    "黑神话悟空": "blackmythwukong", "黑神话": "blackmythwukong", "悟空": "blackmythwukong", "blackmythwukong": "blackmythwukong",
    "上古卷轴5": "skyrimspecialedition", "老滚5": "skyrimspecialedition", "天际": "skyrimspecialedition", "skyrimspecialedition": "skyrimspecialedition",
    "巫师3": "witcher3", "巫师": "witcher3", "witcher3": "witcher3",
    "环世界": "rimworld", "边缘世界": "rimworld", "rimworld": "rimworld",
    "杀戮尖塔": "slaythespire", "slaythespire": "slaythespire",
    "怪物猎人世界": "monsterhunterworld", "怪猎世界": "monsterhunterworld", "monsterhunterworld": "monsterhunterworld",
    "辐射4": "fallout4", "fallout4": "fallout4",
    "空洞骑士": "hollowknight", "hollowknight": "hollowknight", "丝之歌": "hollowknightsilksong", "hollowknightsilksong": "hollowknightsilksong",
    "饥荒": "dontstarvetogether", "饥荒联机版": "dontstarvetogether", "dontstarvetogether": "dontstarvetogether",
    "匹诺曹的谎言": "liesofp", "liesofp": "liesofp",
    "僵尸毁灭工程": "projectzomboid", "projectzomboid": "projectzomboid",
    "艾尔登法环": "eldenring", "老头环": "eldenring", "eldenring": "eldenring",
    "博德之门3": "baldursgate3", "bg3": "baldursgate3", "baldursgate3": "baldursgate3",
    "gta5": "grandtheftautov", "给他爱5": "grandtheftautov", "grandtheftautov": "grandtheftautov"
}

DEFAULT_ENVIRONMENT = {
    "chromePath": "auto",
    "chromeProfileDir": "./.chrome_mod_profile",
    "cdpPort": 9222,
    "downloadsDir": "auto",
    "extraDownloadDirs": [],
    "tmpDownloadsDir": "./tmp_downloads",
    "backupDir": "./deleted_mods",
    "installedModsDir": "./installed_mods",
    "mappingsFile": "./mod_mappings.json"
}


def load_config(custom_config_path: Optional[str] = None) -> Dict[str, Any]:
    """统一加载 mod_config.json，自动补全默认环境配置与归一化路径"""
    cfg_file = None
    if custom_config_path and Path(custom_config_path).exists():
        cfg_file = Path(custom_config_path)
    elif os.environ.get("MOD_CONFIG_PATH") and Path(os.environ.get("MOD_CONFIG_PATH")).exists():
        cfg_file = Path(os.environ.get("MOD_CONFIG_PATH"))
    elif PROJECT_CONFIG.exists():
        cfg_file = PROJECT_CONFIG
    elif (Path.cwd() / "mod_config.json").exists():
        cfg_file = Path.cwd() / "mod_config.json"
    elif APPDATA_CONFIG.exists():
        cfg_file = APPDATA_CONFIG

    cfg = {}
    if cfg_file and cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[警告] 解析配置文件 {cfg_file} 失败: {e}，将使用内存默认值")

    # 注入/补全 environment 节点
    env_cfg = cfg.get("environment", {})
    for k, v in DEFAULT_ENVIRONMENT.items():
        if k not in env_cfg:
            env_cfg[k] = list(v) if isinstance(v, list) else v
    cfg["environment"] = env_cfg

    # 向后兼容顶层 tmpFolderPath
    if "tmpFolderPath" not in cfg:
        cfg["tmpFolderPath"] = env_cfg.get("tmpDownloadsDir", "./tmp_downloads")

    return cfg


def save_config(cfg: Dict[str, Any], target_path: Optional[Path] = None) -> None:
    """持久化保存 mod_config.json"""
    dest = target_path or PROJECT_CONFIG
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[警告] 无法保存配置至 {dest}: {e}")


def _resolve_dir(raw_path: str, base_dir: Path) -> Path:
    """解析相对/绝对路径为标准绝对路径"""
    p = Path(raw_path)
    if not p.is_absolute():
        p = (base_dir / p).resolve()
    return p


def detect_chrome_download_dir(profile_dir: Optional[Path] = None) -> Optional[Path]:
    """读取 Chrome Profile 或本机默认 Chrome Preferences 中的 download.default_directory"""
    candidates = []
    if profile_dir:
        candidates.append(Path(profile_dir) / "Default" / "Preferences")
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        candidates.append(Path(local_app) / "Google" / "Chrome" / "User Data" / "Default" / "Preferences")

    for pref in candidates:
        if pref.exists():
            try:
                data = json.loads(pref.read_text(encoding="utf-8", errors="ignore"))
                dl_dir = data.get("download", {}).get("default_directory")
                if dl_dir:
                    p = Path(dl_dir).expanduser()
                    if p.exists():
                        return p.resolve()
            except Exception:
                pass
    return None


def get_system_paths(cfg: Optional[Dict[str, Any]] = None, root_override: Optional[Path] = None) -> Dict[str, Any]:
    """
    根据配置解析系统中所需的各种路径。
    支持 'auto' 探测机制，跨电脑自动适配。
    """
    if cfg is None:
        cfg = load_config()

    root = Path(root_override).resolve() if root_override else find_project_root()
    env = cfg.get("environment", DEFAULT_ENVIRONMENT)

    # 1. 解析 Chrome 可执行文件路径
    chrome_cfg = env.get("chromePath", "auto")
    chrome_exe = None
    if chrome_cfg and chrome_cfg != "auto" and Path(chrome_cfg).exists():
        chrome_exe = str(Path(chrome_cfg).resolve())
    else:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
            shutil.which("chrome") or "",
            shutil.which("chrome.exe") or "",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for c in candidates:
            if c and Path(c).exists():
                chrome_exe = str(Path(c).resolve())
                break
        if not chrome_exe:
            chrome_exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

    # 2. 解析 Chrome 专用 Profile 目录
    prof_cfg = env.get("chromeProfileDir", "./.chrome_mod_profile")
    if prof_cfg.startswith("~/") or prof_cfg.startswith("~\\"):
        chrome_profile_dir = (Path.home() / prof_cfg[2:]).resolve()
    else:
        chrome_profile_dir = _resolve_dir(prof_cfg, root)
    chrome_profile_dir.mkdir(parents=True, exist_ok=True)

    # 3. 解析用户下载目录 (Downloads)
    dl_cfg = env.get("downloadsDir", "auto")
    if dl_cfg and dl_cfg != "auto" and Path(dl_cfg).exists():
        downloads_dir = Path(dl_cfg).resolve()
    else:
        downloads_dir = (Path.home() / "Downloads").resolve()

    # 4. 解析项目临时下载目录 (tmp_downloads)
    tmp_cfg = env.get("tmpDownloadsDir", cfg.get("tmpFolderPath", "./tmp_downloads"))
    tmp_downloads_dir = _resolve_dir(tmp_cfg, root)
    tmp_downloads_dir.mkdir(parents=True, exist_ok=True)

    # 5. 解析模组软删除备份归档目录 (deleted_mods)
    backup_cfg = env.get("backupDir", "./deleted_mods")
    backup_dir = _resolve_dir(backup_cfg, root)
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 6. 解析已安装模组清单分文件存储目录 (installed_mods)
    inst_cfg = env.get("installedModsDir", "./installed_mods")
    installed_mods_dir = _resolve_dir(inst_cfg, root)
    installed_mods_dir.mkdir(parents=True, exist_ok=True)

    # 7. 解析规则与映射文件 (mod_mappings.json)
    map_cfg = env.get("mappingsFile", "./mod_mappings.json")
    mappings_file = _resolve_dir(map_cfg, root)

    # 8. CDP 调试端口
    cdp_port = int(env.get("cdpPort", 9222))

    # 9. 汇总所有下载监控目录 (tmp_downloads + 系统 Downloads + extraDownloadDirs + Chrome 偏好下载目录)
    all_download_dirs: List[Path] = []
    seen_dirs: Set[str] = set()

    def _add_dl_dir(p: Optional[Path]) -> None:
        if not p:
            return
        try:
            rp = p.resolve()
            key = str(rp).lower()
            if key not in seen_dirs:
                seen_dirs.add(key)
                all_download_dirs.append(rp)
        except Exception:
            pass

    _add_dl_dir(tmp_downloads_dir)
    _add_dl_dir(downloads_dir)
    for raw_extra in env.get("extraDownloadDirs", []) or []:
        if raw_extra:
            _add_dl_dir(_resolve_dir(str(raw_extra), root))
    _add_dl_dir(detect_chrome_download_dir(chrome_profile_dir))

    return {
        "project_root": root,
        "chrome_exe": chrome_exe,
        "chrome_profile_dir": chrome_profile_dir,
        "downloads_dir": downloads_dir,
        "tmp_downloads_dir": tmp_downloads_dir,
        "all_download_dirs": all_download_dirs,
        "backup_dir": backup_dir,
        "installed_mods_dir": installed_mods_dir,
        "mappings_file": mappings_file,
        "cdp_port": cdp_port
    }


def write_mcp_configs(project_root: Path, chrome_profile_dir: Path) -> None:
    """根据当前项目环境自动生成/更新根目录 .mcp.json 与 mcp_config.json"""
    try:
        rel_profile = chrome_profile_dir.resolve().relative_to(project_root.resolve())
        profile_posix = f"./{rel_profile.as_posix()}"
    except Exception:
        profile_posix = str(chrome_profile_dir.resolve()).replace("\\", "/")
    mcp_payload = {
        "mcpServers": {
            "chrome-devtools": {
                "command": "npx.cmd",
                "args": [
                    "-y",
                    "chrome-devtools-mcp@latest",
                    f"--userDataDir={profile_posix}",
                    "--chrome-arg=--disable-blink-features=AutomationControlled",
                    "--ignore-default-chrome-arg=--enable-automation"
                ]
            }
        }
    }
    text = json.dumps(mcp_payload, indent=2, ensure_ascii=False) + "\n"
    for fname in [".mcp.json", "mcp_config.json"]:
        target = project_root / fname
        try:
            target.write_text(text, encoding="utf-8")
        except Exception as e:
            print(f"[警告] 无法写入 MCP 配置 {target}: {e}")


def init_project_environment(
    cfg: Optional[Dict[str, Any]] = None,
    project_root: Optional[Path] = None,
    save_mcp: bool = True,
    save_cfg: bool = True
) -> Dict[str, Any]:
    """
    初始化项目运行环境与所有相关配置：
    1. 补全 mod_config.json 的 environment 节点并自动探测额外下载目录 (extraDownloadDirs)；
    2. 创建运行时目录 (.chrome_mod_profile, tmp_downloads, deleted_mods, installed_mods)；
    3. 自动生成/同步根目录 .mcp.json 与 mcp_config.json，消除跨机硬编码路径。
    """
    root = Path(project_root).resolve() if project_root else find_project_root()
    if cfg is None:
        cfg = load_config(str(root / "mod_config.json"))

    env_cfg = cfg.setdefault("environment", {})
    for k, v in DEFAULT_ENVIRONMENT.items():
        if k not in env_cfg:
            env_cfg[k] = list(v) if isinstance(v, list) else v

    if "tmpFolderPath" not in cfg:
        cfg["tmpFolderPath"] = env_cfg.get("tmpDownloadsDir", "./tmp_downloads")

    paths = get_system_paths(cfg, root_override=root)

    # 自动探测 Chrome 实际下载目录并记录到 environment.extraDownloadDirs
    detected_dl = detect_chrome_download_dir(paths["chrome_profile_dir"])
    extra_list = [str(x).replace("\\", "/") for x in (env_cfg.get("extraDownloadDirs") or []) if x]
    if detected_dl and detected_dl.resolve() != paths["downloads_dir"].resolve():
        dl_posix = str(detected_dl.resolve()).replace("\\", "/")
        if dl_posix.lower() not in {x.lower() for x in extra_list}:
            extra_list.append(dl_posix)
    env_cfg["extraDownloadDirs"] = extra_list

    if save_mcp:
        write_mcp_configs(root, paths["chrome_profile_dir"])

    if save_cfg:
        save_config(cfg, root / "mod_config.json")

    return cfg


def normalize_game_domain(name_or_domain: str) -> str:
    """归一化游戏名称或别名为标准 gameDomain（叠加静态别名表与 mod_mappings.json 中的学习别名表）"""
    clean = str(name_or_domain).strip().lower()
    if clean in GAME_ALIAS_MAP:
        return GAME_ALIAS_MAP[clean]
    learned_aliases = load_mappings().get("game_aliases", {})
    if clean in learned_aliases:
        return str(learned_aliases[clean]).strip().lower()
    return clean


def get_nexus_domain(game_domain: str) -> str:
    """获取官方 Nexus Mods API / 网页所对应的标准游戏域名"""
    d = normalize_game_domain(game_domain)
    if d == "phantomblandzero":
        return "phantombladezero"
    return d


def get_game_config(cfg: Dict[str, Any], game_input: str) -> Dict[str, Any]:
    """提取特定游戏的子配置，向下兼容扁平配置"""
    domain = normalize_game_domain(game_input)
    games = cfg.get("games", {})
    game_sub = games.get(domain, {})
    if not game_sub:
        for k, v in games.items():
            if v.get("gameDomain") == domain:
                game_sub = v
                break

    merged = dict(cfg)
    merged.update(game_sub)
    merged["gameDomain"] = domain
    return merged


def resolve_api_key(arg: Optional[str], cfg: Dict[str, Any]) -> str:
    """提取有效的 Nexus API Key"""
    for src in [arg, os.environ.get("NEXUS_API_KEY"), cfg.get("nexusApiKey")]:
        if src:
            return str(src).strip().strip('"').strip("'")
    return ""


def resolve_mods_path(custom_mods_path: Optional[str], game_domain: str, cfg: Dict[str, Any]) -> Path:
    """统一解析游戏模组目录"""
    if custom_mods_path and Path(custom_mods_path).exists():
        return Path(custom_mods_path).resolve()

    gcfg = get_game_config(cfg, game_domain)
    mpath = gcfg.get("modsPath")
    if mpath and Path(mpath).exists():
        return Path(mpath).resolve()

    gpath = gcfg.get("gamePath")
    if gpath and Path(gpath).exists():
        profile = get_game_profiles().get(normalize_game_domain(game_domain), {})
        mods_sub = profile.get("mods_subdir") or "Mods"
        cand = Path(gpath) / mods_sub
        if cand.exists():
            return cand.resolve()
        return cand.resolve()

    if mpath:
        return Path(mpath).resolve()
    return Path("Mods").resolve()


def resolve_game_root(game_domain: str, cfg: Optional[Dict[str, Any]] = None, mods_path: Optional[Path] = None) -> Path:
    """
    精准解析游戏的真实根目录（gamePath）：
    避免对多级 mods_subdir（如 archive/pc/mod 或 platform/steam/ue4ss/Mods）错误使用 mods_path.parent。
    """
    if cfg is None:
        cfg = load_config()
    domain = normalize_game_domain(game_domain)
    gcfg = get_game_config(cfg, domain)
    gpath = gcfg.get("gamePath")
    if gpath and Path(gpath).exists():
        return Path(gpath).resolve()

    if mods_path:
        mp = Path(mods_path).resolve()
        profile = get_game_profiles().get(domain, {})
        mods_sub = (profile.get("mods_subdir") or "Mods").replace("\\", "/").strip("/")
        sub_parts = [p.lower() for p in mods_sub.split("/") if p]
        mp_parts = [p.lower() for p in mp.parts]
        if sub_parts and len(mp_parts) > len(sub_parts) and mp_parts[-len(sub_parts):] == sub_parts:
            root_cand = mp
            for _ in sub_parts:
                root_cand = root_cand.parent
            return root_cand
        # 特判常见多级目录结构
        if mp.name.lower() == "mod" and len(mp.parents) >= 3 and mp.parent.name.lower() == "pc" and mp.parents[1].name.lower() == "archive":
            return mp.parents[2]
        if mp.name.lower() == "mods" and len(mp.parents) >= 4 and mp.parent.name.lower() == "ue4ss":
            return mp.parents[3]
        if mp.name.lower() == "~mods" and len(mp.parents) >= 4 and mp.parent.name.lower() == "paks":
            return mp.parents[3]
        return mp.parent

    if gpath:
        return Path(gpath).resolve()
    return Path.cwd()


def parse_nexus_reference(raw_input: str) -> Optional[Dict[str, Any]]:
    """
    统一识别并解析：
      1. Nexus 网页链接: https://www.nexusmods.com/<game>/mods/<mod_id>[?tab=files&file_id=<file_id>]
      2. NXM 协议链接: nxm://<game>/mods/<mod_id>/files/<file_id>?key=...&expires=...
    返回标准化字典: {"type": "web"|"nxm", "game_domain": ..., "mod_id": int, "file_id": Optional[int], "key": ..., "expires": ...}
    """
    if not raw_input:
        return None
    import re
    from urllib.parse import urlparse, parse_qs

    text = raw_input.strip()
    if text.lower().startswith("nxm://"):
        p = urlparse(text)
        raw_dom = normalize_game_domain(p.netloc)
        parts = [x for x in p.path.split("/") if x]
        mod_id = None
        file_id = None
        if len(parts) >= 4 and parts[0].lower() == "mods" and parts[2].lower() == "files":
            try:
                mod_id = int(parts[1])
                file_id = int(parts[3])
            except ValueError:
                pass
        qs = parse_qs(p.query)
        return {
            "type": "nxm",
            "game_domain": raw_dom,
            "mod_id": mod_id,
            "file_id": file_id,
            "key": qs.get("key", [None])[0],
            "expires": qs.get("expires", [None])[0]
        }

    m = re.search(r"nexusmods\.com/([a-zA-Z0-9_-]+)/mods/(\d+)", text, re.I)
    if m:
        raw_dom = normalize_game_domain(m.group(1))
        mod_id = int(m.group(2))
        file_id = None
        try:
            qs = parse_qs(urlparse(text).query)
            if "file_id" in qs and qs["file_id"][0].isdigit():
                file_id = int(qs["file_id"][0])
        except Exception:
            pass
        return {
            "type": "web",
            "game_domain": raw_dom,
            "mod_id": mod_id,
            "file_id": file_id,
            "key": None,
            "expires": None
        }
    return None


# ==============================================================================
# 集中化规则与对照映射表管理 (mod_mappings.json)
# ==============================================================================

_MAPPINGS_CACHE: Optional[Dict[str, Any]] = None
_MAPPINGS_MTIME: float = 0.0
_MAPPINGS_PATH_KEY: str = ""


def load_mappings(custom_mappings_path: Optional[str] = None) -> Dict[str, Any]:
    """
    统一读取 mod_mappings.json 单一数据源字典配置。
    具备基于文件 mtime 的智能热加载能力，直接读取 PROJECT_ROOT/mod_mappings.json 避免循环依赖。
    """
    global _MAPPINGS_CACHE, _MAPPINGS_MTIME, _MAPPINGS_PATH_KEY

    target_path = Path(custom_mappings_path) if custom_mappings_path else MAPPINGS_FILE
    path_key = str(target_path)

    if target_path and target_path.exists():
        try:
            mtime = target_path.stat().st_mtime
            if _MAPPINGS_CACHE is not None and _MAPPINGS_PATH_KEY == path_key and mtime == _MAPPINGS_MTIME:
                return _MAPPINGS_CACHE
            data = json.loads(target_path.read_text(encoding="utf-8"))
            _MAPPINGS_CACHE = data
            _MAPPINGS_MTIME = mtime
            _MAPPINGS_PATH_KEY = path_key
            return data
        except Exception as e:
            print(f"[警告] 读取映射文件 {target_path} 异常: {e}，将使用内存兜底")

    if _MAPPINGS_CACHE is not None and _MAPPINGS_PATH_KEY == path_key:
        return _MAPPINGS_CACHE

    return {
        "game_profiles": {},
        "game_aliases": {},
        "translations": {},
        "ue4ss": {"builtin_folders": []},
        "search_keywords": {},
        "upstream_mirrors": {}
    }


def save_mappings(data: Dict[str, Any], custom_mappings_path: Optional[Path] = None) -> Path:
    """持久化更新 mod_mappings.json 单一数据源并刷新热缓存"""
    global _MAPPINGS_CACHE, _MAPPINGS_MTIME, _MAPPINGS_PATH_KEY
    target = Path(custom_mappings_path) if custom_mappings_path else MAPPINGS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _MAPPINGS_CACHE = data
    _MAPPINGS_MTIME = target.stat().st_mtime
    _MAPPINGS_PATH_KEY = str(target)
    return target


def learn_game_profile(
    game_domain: str,
    profile_data: Dict[str, Any],
    custom_mappings_path: Optional[Path] = None
) -> Dict[str, Any]:
    """合并并持久化指定游戏的自适应安装规则至 mod_mappings.json"""
    import datetime
    domain = str(game_domain).strip().lower()
    data = load_mappings(str(custom_mappings_path) if custom_mappings_path else None)
    gp = data.setdefault("game_profiles", {})
    existing = dict(gp.get(domain, {}))

    merged_sigs: List[str] = []
    for sig in (existing.get("signature_files") or []) + (profile_data.get("signature_files") or []):
        if sig and sig not in merged_sigs:
            merged_sigs.append(sig)

    merged = dict(existing)
    for k, v in profile_data.items():
        if v is not None and v != "":
            merged[k] = v
    if merged_sigs:
        merged["signature_files"] = merged_sigs
    merged["learned_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    gp[domain] = merged
    save_mappings(data, custom_mappings_path)
    return merged


def learn_game_alias(alias: str, game_domain: str, custom_mappings_path: Optional[Path] = None) -> bool:
    """沉淀新游戏的中文名/别名到 mod_mappings.json 的 game_aliases"""
    clean_alias = str(alias or "").strip().lower()
    clean_domain = str(game_domain or "").strip().lower()
    if not clean_alias or not clean_domain or clean_alias == clean_domain:
        return False
    if clean_alias in GAME_ALIAS_MAP:
        return False
    data = load_mappings(str(custom_mappings_path) if custom_mappings_path else None)
    aliases = data.setdefault("game_aliases", {})
    if aliases.get(clean_alias) == clean_domain:
        return False
    aliases[clean_alias] = clean_domain
    save_mappings(data, custom_mappings_path)
    return True


def learn_search_keyword(
    game_domain: str,
    zh_query: str,
    en_term: str,
    mod_name: str = "",
    custom_mappings_path: Optional[Path] = None
) -> bool:
    """安装成功后反哺中文搜索意图词与模组中文名至 mod_mappings.json，后续检索直接命中"""
    q = str(zh_query or "").strip()
    en = str(en_term or "").strip()
    if not q or not en or q.lower() == en.lower():
        return False
    if not any("\u4e00" <= ch <= "\u9fff" for ch in q):
        return False
    if len(q) > 32:
        return False

    domain = normalize_game_domain(game_domain)
    data = load_mappings(str(custom_mappings_path) if custom_mappings_path else None)
    sk = data.setdefault("search_keywords", {})
    if q in sk.get(domain, {}) or q in sk.get("general", {}):
        return False

    game_sk = sk.setdefault(domain, {})
    changed = False
    if game_sk.get(q) != en:
        game_sk[q] = en
        changed = True

    if mod_name and not any("\u4e00" <= ch <= "\u9fff" for ch in mod_name):
        game_mn = data.setdefault("translations", {}).setdefault(domain, {}).setdefault("mod_names", {})
        if mod_name not in game_mn:
            game_mn[mod_name] = f"{q} ({mod_name})"
            changed = True

    if changed:
        save_mappings(data, custom_mappings_path)
    return changed


def get_game_profiles() -> Dict[str, Any]:
    """获取所有游戏的安装规则与特征 Profile 字典"""
    return dict(load_mappings().get("game_profiles", {}))


def get_mod_translations(game_domain: str = "stardewvalley") -> Dict[str, Any]:
    """获取指定游戏的中文名称、NPC 对照与关键词映射表"""
    domain = normalize_game_domain(game_domain)
    return dict(load_mappings().get("translations", {}).get(domain, {}))


def get_ue4ss_builtin_folders() -> Set[str]:
    """获取 UE4SS 官方框架内置组件目录集合"""
    folders = load_mappings().get("ue4ss", {}).get("builtin_folders", [])
    return {f.lower() for f in folders}


def get_game_mod_metadata(game_domain: str, mod_key: str) -> Optional[Dict[str, Any]]:
    """获取特定游戏中模组的预置官方元数据（Nexus ID、中文名、版本等）"""
    domain = normalize_game_domain(game_domain)
    clean_k = str(mod_key).strip().lower()
    meta_dict = load_mappings().get("translations", {}).get(domain, {}).get("mod_metadata", {})
    return meta_dict.get(clean_k)


def get_search_keywords(game_domain: str = "stardewvalley") -> Dict[str, Dict[str, str]]:
    """获取指定游戏与通用的中文搜索关键词映射字典"""
    domain = normalize_game_domain(game_domain)
    sk = load_mappings().get("search_keywords", {})
    return {
        "game": dict(sk.get(domain, {})),
        "general": dict(sk.get("general", {}))
    }


def get_upstream_mirrors() -> Dict[str, Dict[str, Any]]:
    """获取已验证的官方开源/GitHub Release 镜像配置表"""
    return load_mappings().get("upstream_mirrors", {})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="集中化配置与项目环境初始化工具")
    ap.add_argument("--init", action="store_true", help="初始化/同步 mod_config.json、.mcp.json、mcp_config.json 及运行时目录")
    ap.add_argument("--show-paths", action="store_true", help="输出当前解析后的全部系统路径")
    args = ap.parse_args()

    if args.init:
        updated = init_project_environment()
        print("✓ 项目环境与配置初始化完成！")
        print(json.dumps(updated.get("environment", {}), indent=2, ensure_ascii=False))
    elif args.show_paths:
        p = get_system_paths()
        print(json.dumps({k: [str(x) for x in v] if isinstance(v, list) else str(v) for k, v in p.items()}, indent=2, ensure_ascii=False))
    else:
        ap.print_help()
