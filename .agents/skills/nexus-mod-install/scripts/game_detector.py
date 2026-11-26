"""
game_detector.py — 本地真实已安装游戏智能探测与按需配置引擎

核心原则：
  1. 默认精简初始化：绝不无脑导入离线/未挂载外部库及无关日常软件，保持 mod_config.json 纯净。
  2. 按需定向探测：当后续指定安装某款游戏时，再动态扫描该游戏的本地真实安装路径并追加写入 mod_config.json。
  3. 中英文语义别名映射：支持中文（如“星露谷物语”、“赛博朋克2077”、“黑神话悟空”、“荒野大镖客2”）自动对齐至 Nexus 规范。
"""

import json
import os
import re
import sys
import winreg
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

STEAM_APPID_MAP = {
    "413150": ("stardewvalley", "Mods", "Stardew Valley", 1303),
    "1091500": ("cyberpunk2077", "archive/pc/mod", "Cyberpunk 2077", 3333),
    "1174180": ("reddeadredemption2", "lml", "Red Dead Redemption 2", 3024),
    "4115450": ("phantomblandzero", "platform/steam/ue4ss/Mods", "Phantom Blade Zero", 10614),
    "292030": ("witcher3", "mods", "The Witcher 3: Wild Hunt", 952),
    "271590": ("grandtheftautov", "mods", "Grand Theft Auto V", 275),
    "582010": ("monsterhunterworld", "nativePC", "Monster Hunter: World", 2531),
    "397540": ("borderlands3", "OakGame/Content/Paks/~mods", "Borderlands 3", 2933),
    "2358720": ("blackmythwukong", "b1/Content/Paks/~mods", "Black Myth: Wukong", 6451),
    "489830": ("skyrimspecialedition", "Data", "Skyrim Special Edition", 1704),
    "377160": ("fallout4", "Data", "Fallout 4", 1151),
    "294100": ("rimworld", "Mods", "RimWorld", 1824),
    "646570": ("slaythespire", "mods", "Slay the Spire", 2835),
    "632360": ("riskofrain2", "BepInEx/plugins", "Risk of Rain 2", 2841),
    "281990": ("stellaris", "mod", "Stellaris", 1528),
    "108600": ("projectzomboid", "mods", "Project Zomboid", 481),
    "1222670": ("detroitbecomehuman", "mods", "Detroit: Become Human", 3254),
    "367520": ("hollowknight", "hollow_knight_Data/Managed/Mods", "Hollow Knight", 3016),
    "1030300": ("hollowknightsilksong", "Mods", "Hollow Knight Silksong", None),
    "1145360": ("hades", "Content/Mods", "Hades", 3042),
    "322330": ("dontstarvetogether", "mods", "Don't Starve Together", 328),
    "588650": ("deadcells", "Mods", "Dead Cells", 2623),
    "683320": ("chineseparents", "Mods", "Chinese Parents (中国式家长)", None),
    "962130": ("bloonstd6", "Mods", "Bloons TD 6", 3619),
    "1928420": ("vpet", "Mods", "VPet", None),
    "1426210": ("ittakestwo", "Mods", "It Takes Two", None),
    "1313140": ("cultofthelamb", "BepInEx/plugins", "Cult of the Lamb", 4880),
    "1147560": ("vampiresurvivors", "resources/app/.webpack/renderer", "Vampire Survivors", 4236),
    "1293830": ("forzahorizon4", "Mods", "Forza Horizon 4", None),
    "282070": ("thiswarofmine", "Mods", "This War of Mine", 685),
    "518790": ("thehuntercallofthewild", "dropzone", "theHunter: Call of the Wild", 1888),
    "2001120": ("liesofp", "Content/Paks/~mods", "Lies of P", 5521),
}

KNOWN_NAME_KEYWORDS = {
    "stardew valley": ("stardewvalley", "Mods", "Stardew Valley", 1303),
    "stardew": ("stardewvalley", "Mods", "Stardew Valley", 1303),
    "rimworld": ("rimworld", "Mods", "RimWorld", 1824),
    "cyberpunk": ("cyberpunk2077", "archive/pc/mod", "Cyberpunk 2077", 3333),
    "red dead": ("reddeadredemption2", "lml", "Red Dead Redemption 2", 3024),
    "grand theft auto": ("grandtheftautov", "mods", "Grand Theft Auto V", 275),
    "witcher 3": ("witcher3", "mods", "The Witcher 3: Wild Hunt", 952),
    "monster hunter": ("monsterhunterworld", "nativePC", "Monster Hunter: World", 2531),
    "skyrim": ("skyrimspecialedition", "Data", "Skyrim Special Edition", 1704),
    "fallout 4": ("fallout4", "Data", "Fallout 4", 1151),
    "fallout": ("fallout4", "Data", "Fallout 4", 1151),
    "wukong": ("blackmythwukong", "b1/Content/Paks/~mods", "Black Myth: Wukong", 6451),
    "black myth": ("blackmythwukong", "b1/Content/Paks/~mods", "Black Myth: Wukong", 6451),
    "bloons": ("bloonstd6", "Mods", "Bloons TD 6", 3619),
    "slay the spire": ("slaythespire", "mods", "Slay the Spire", 2835),
    "slaythespire": ("slaythespire", "mods", "Slay the Spire", 2835),
    "risk of rain": ("riskofrain2", "BepInEx/plugins", "Risk of Rain 2", 2841),
    "stellaris": ("stellaris", "mod", "Stellaris", 1528),
    "borderlands 3": ("borderlands3", "OakGame/Content/Paks/~mods", "Borderlands 3", 2933),
    "borderlands": ("borderlands3", "OakGame/Content/Paks/~mods", "Borderlands 3", 2933),
    "detroit": ("detroitbecomehuman", "mods", "Detroit: Become Human", 3254),
    "phantom blade": ("phantomblandzero", "platform/steam/ue4ss/Mods", "Phantom Blade Zero", None),
    "pbz": ("phantomblandzero", "platform/steam/ue4ss/Mods", "Phantom Blade Zero", None),
    "影之刃": ("phantomblandzero", "platform/steam/ue4ss/Mods", "Phantom Blade Zero", None),
    "silksong": ("hollowknightsilksong", "Mods", "Hollow Knight Silksong", None),
    "hollow knight": ("hollowknight", "hollow_knight_Data/Managed/Mods", "Hollow Knight", 3016),
    "hades": ("hades", "Content/Mods", "Hades", 3042),
    "dead cells": ("deadcells", "Mods", "Dead Cells", 2623),
    "lies of p": ("liesofp", "Content/Paks/~mods", "Lies of P", 5521),
    "project zomboid": ("projectzomboid", "mods", "Project Zomboid", 481),
}

# 中文及别名精准映射表 -> 游戏规范 domain
GAME_ALIAS_MAP = {
    # 星露谷物语
    "星露谷物语": "stardewvalley",
    "星露谷": "stardewvalley",
    "stardew": "stardewvalley",
    "stardewvalley": "stardewvalley",
    # 赛博朋克2077
    "赛博朋克2077": "cyberpunk2077",
    "赛博朋克": "cyberpunk2077",
    "2077": "cyberpunk2077",
    "cyberpunk": "cyberpunk2077",
    "cyberpunk2077": "cyberpunk2077",
    # 荒野大镖客2
    "荒野大镖客2": "reddeadredemption2",
    "荒野大镖客": "reddeadredemption2",
    "大镖客2": "reddeadredemption2",
    "大镖客": "reddeadredemption2",
    "rdr2": "reddeadredemption2",
    "reddeadredemption2": "reddeadredemption2",
    # 黑神话悟空
    "黑神话悟空": "blackmythwukong",
    "黑神话": "blackmythwukong",
    "悟空": "blackmythwukong",
    "blackmythwukong": "blackmythwukong",
    # 影之刃零
    "影之刃零": "phantomblandzero",
    "影之刃": "phantomblandzero",
    "pbz": "phantomblandzero",
    "phantombladezero": "phantomblandzero",
    "phantom blade zero": "phantomblandzero",
    "phantom blade": "phantomblandzero",
    "phantomblandzero": "phantomblandzero",
    # 环世界
    "环世界": "rimworld",
    "边缘世界": "rimworld",
    "rimworld": "rimworld",
    # 杀戮尖塔
    "杀戮尖塔": "slaythespire",
    "slaythespire": "slaythespire",
    # 巫师3
    "巫师3": "witcher3",
    "巫师": "witcher3",
    "witcher3": "witcher3",
    # 怪物猎人世界
    "怪物猎人世界": "monsterhunterworld",
    "怪猎世界": "monsterhunterworld",
    "monsterhunterworld": "monsterhunterworld",
    # 上古卷轴5
    "上古卷轴5": "skyrimspecialedition",
    "老滚5": "skyrimspecialedition",
    "天际": "skyrimspecialedition",
    "skyrimspecialedition": "skyrimspecialedition",
    # 辐射4
    "辐射4": "fallout4",
    "fallout4": "fallout4",
    # 空洞骑士
    "空洞骑士": "hollowknight",
    "hollowknight": "hollowknight",
    "丝之歌": "hollowknightsilksong",
    "hollowknightsilksong": "hollowknightsilksong",
    # 饥荒
    "饥荒联机版": "dontstarvetogether",
    "饥荒": "dontstarvetogether",
    "dontstarvetogether": "dontstarvetogether",
    # 匹诺曹的谎言
    "匹诺曹的谎言": "liesofp",
    "匹诺曹": "liesofp",
    "liesofp": "liesofp",
    # 僵尸毁灭工程
    "僵尸毁灭工程": "projectzomboid",
    "projectzomboid": "projectzomboid",
}

IGNORED_COMMON_APPS = {
    "steamworks shared", "steam controller configs", "launcher", "directxredist",
    "git", "7-zip", "office", "intellij", "pycharm", "visual studio",
    "chrome", "edge", "copilot", "wechat", "weixin", "qq", "winrar", "typora",
    "mysql", "pandoc", "everythingtoolbar", "cisco", "mindmaster", "bun",
    "图吧工具箱", "rk keyboard", "antigravity", "nvm", "redlauncher", "steamworks common redistributables",
    # 默认过滤普通桌面小工具与无常规模组目录的小游戏
    "vpet", "dungeonvillage", "gamedevstory", "highseasagadx"
}


class GameDetector:
    def __init__(self):
        self.steam_libraries: List[Dict[str, Any]] = self._discover_steam_libraries()

    def _discover_steam_libraries(self) -> List[Dict[str, Any]]:
        """发现所有 Steam 真实已挂载生效的库"""
        libraries = []
        steam_paths = set()

        for hkey, subkey in [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam")
        ]:
            try:
                with winreg.OpenKey(hkey, subkey, 0, winreg.KEY_READ) as k:
                    for vname in ("SteamPath", "InstallPath"):
                        try:
                            val, _ = winreg.QueryValueEx(k, vname)
                            if val:
                                p = Path(val)
                                if p.exists():
                                    steam_paths.add(p)
                        except Exception:
                            pass
            except Exception:
                pass

        # 默认常见 Steam 安装目录回退
        for def_p in [
            Path(r"C:\Program Files (x86)\Steam"),
            Path(r"C:\Program Files\Steam"),
        ]:
            if def_p.exists():
                steam_paths.add(def_p)

        # 扫描所有物理驱动器盘符下的常见 SteamLibrary
        for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive_root = Path(f"{drive}:\\")
            if drive_root.exists():
                for candidate in [drive_root / "SteamLibrary", drive_root / "Steam"]:
                    if candidate.exists() and (candidate / "steamapps").exists():
                        if not any(lib["path"] == candidate for lib in libraries):
                            libraries.append({"path": candidate, "exists": True})

        for sp in steam_paths:
            vdf = sp / "steamapps" / "libraryfolders.vdf"
            if vdf.exists():
                try:
                    text = vdf.read_text(encoding="utf-8", errors="ignore")
                    blocks = re.split(r'"\d+"\s*\{', text)
                    for block in blocks[1:]:
                        path_m = re.search(r'"path"\s+"([^"]+)"', block)
                        if not path_m:
                            continue
                        raw_p = path_m.group(1).replace("\\\\", "\\")
                        lib_path = Path(raw_p)
                        # 仅记录真实存在的盘符与库
                        if lib_path.exists() and not any(lib["path"] == lib_path for lib in libraries):
                            libraries.append({
                                "path": lib_path,
                                "exists": True
                            })
                except Exception:
                    pass
            if sp.exists() and not any(lib["path"] == sp for lib in libraries):
                libraries.append({"path": sp, "exists": True})
        return libraries

    def scan_steam_games(self) -> List[Dict[str, Any]]:
        """扫描真实已挂载 Steam 库中切实存在的游戏"""
        games = []
        for lib_info in self.steam_libraries:
            lib = lib_info["path"]
            steamapps_dir = lib / "steamapps"
            common_dir = steamapps_dir / "common"

            if not steamapps_dir.exists():
                continue

            # 1. 扫描真实 appmanifest_*.acf
            for acf in steamapps_dir.glob("appmanifest_*.acf"):
                try:
                    text = acf.read_text(encoding="utf-8", errors="ignore")
                    name_m = re.search(r'"name"\s+"([^"]+)"', text)
                    dir_m = re.search(r'"installdir"\s+"([^"]+)"', text)
                    appid_m = re.search(r'"appid"\s+"([^"]+)"', text)
                    if name_m and dir_m:
                        name = name_m.group(1)
                        appid = appid_m.group(1) if appid_m else None
                        gpath = common_dir / dir_m.group(1)
                        if name.lower() in IGNORED_COMMON_APPS:
                            continue
                        if gpath.exists():
                            games.append({
                                "name": name,
                                "appid": appid,
                                "path": gpath,
                                "exists": True,
                                "library": str(lib).replace("\\", "/"),
                                "source": "Steam"
                            })
                except Exception:
                    pass

            # 2. 扫描 common 子目录真实文件夹
            if common_dir.exists():
                for folder in common_dir.iterdir():
                    if folder.is_dir() and not any(g["path"] == folder for g in games):
                        fname = folder.name
                        if fname.lower() in IGNORED_COMMON_APPS:
                            continue
                        # 忽略空目录
                        try:
                            if not any(folder.iterdir()):
                                continue
                        except Exception:
                            pass
                        games.append({
                            "name": fname,
                            "appid": None,
                            "path": folder,
                            "exists": True,
                            "library": str(lib).replace("\\", "/"),
                            "source": "Steam"
                        })
        return games

    def scan_epic_games(self) -> List[Dict[str, Any]]:
        manifest_dir = Path(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests")
        games = []
        if manifest_dir.exists():
            for item in manifest_dir.glob("*.item"):
                try:
                    data = json.loads(item.read_text(encoding="utf-8"))
                    name = data.get("DisplayName")
                    loc = data.get("InstallLocation")
                    if name and loc and name.lower() not in IGNORED_COMMON_APPS:
                        p = Path(loc)
                        if p.exists():
                            games.append({
                                "name": name,
                                "appid": None,
                                "path": p,
                                "exists": True,
                                "library": str(p.parent).replace("\\", "/"),
                                "source": "Epic"
                            })
                except Exception:
                    pass
        return games

    def map_game_profile(self, item: Dict[str, Any]) -> Dict[str, Any]:
        raw_name = item["name"]
        appid = item.get("appid")
        gpath = Path(item["path"])

        domain = None
        mods_sub = "Mods"
        disp_name = raw_name
        nexus_id = None

        if appid and appid in STEAM_APPID_MAP:
            domain, mods_sub, disp_name, nexus_id = STEAM_APPID_MAP[appid]
        else:
            name_lower = raw_name.lower()
            for kw, (d, ms, dn, nid) in KNOWN_NAME_KEYWORDS.items():
                if kw in name_lower or kw in gpath.name.lower():
                    domain, mods_sub, disp_name, nexus_id = d, ms, dn, nid
                    break

        if not domain:
            clean = re.sub(r"[^a-zA-Z0-9]", "", raw_name).lower()
            domain = clean or "customgame"

        mods_path = gpath / mods_sub
        return {
            "name": disp_name,
            "gameDomain": domain,
            "gamePath": str(gpath).replace("\\", "/"),
            "modsPath": str(mods_path).replace("\\", "/"),
            "mods_subdir": mods_sub,
            "nexus_game_id": nexus_id,
            "exists": item["exists"],
            "mods_dir_exists": mods_path.exists() if item["exists"] else False,
            "source": item.get("source"),
            "library": item.get("library")
        }

    def detect_single_game(self, game_query: str) -> Optional[Dict[str, Any]]:
        """
        按需定向探测指定游戏：
        支持输入中文名（如“赛博朋克2077”、“黑神话悟空”、“星露谷物语”）或规范 domain。
        仅在用户需要安装该游戏时才执行全盘检索与自动挂载。
        """
        query_clean = game_query.strip().lower()
        target_domain = GAME_ALIAS_MAP.get(query_clean, query_clean)

        # 1. 先在活跃 Steam 库中匹配
        raw_candidates = self.scan_steam_games() + self.scan_epic_games()
        for raw in raw_candidates:
            prof = self.map_game_profile(raw)
            if prof["gameDomain"] == target_domain or query_clean in prof["name"].lower():
                return prof

        # 2. 如果标准扫描没找到，尝试在已知的 Steam Common 目录定向推算路径
        for lib_info in self.steam_libraries:
            common = lib_info["path"] / "steamapps" / "common"
            if not common.exists():
                continue
            for folder in common.iterdir():
                if not folder.is_dir():
                    continue
                fname_lower = folder.name.lower()
                if target_domain in fname_lower or any(kw in fname_lower for kw in [query_clean]):
                    prof = self.map_game_profile({
                        "name": folder.name,
                        "appid": None,
                        "path": folder,
                        "exists": True,
                        "source": "Steam"
                    })
                    return prof

        # 3. 检查已配置的游戏特征默认路径 (如果目标是已知游戏)
        for aid, (dom, msub, dname, nid) in STEAM_APPID_MAP.items():
            if dom == target_domain:
                # 尝试检查常见默认安装路径
                candidates = [
                    Path(r"C:\Program Files (x86)\Steam\steamapps\common") / dname,
                    Path(r"C:\Program Files\Epic Games") / dname,
                ]
                for c in candidates:
                    if c.exists():
                        prof = {
                            "name": dname,
                            "gameDomain": dom,
                            "gamePath": str(c).replace("\\", "/"),
                            "modsPath": str(c / msub).replace("\\", "/"),
                            "mods_subdir": msub,
                            "nexus_game_id": nid,
                            "exists": True,
                            "mods_dir_exists": (c / msub).exists(),
                            "source": "AutoDetect"
                        }
                        return prof

        return None

    def detect_all(self, only_with_mods: bool = True) -> Dict[str, Any]:
        """精简扫描：保留真实存在且有模组目录或明确为首选目标的游戏"""
        raw_games = self.scan_steam_games() + self.scan_epic_games()
        active = {}
        core_targets = {"stardewvalley", "cyberpunk2077", "reddeadredemption2", "phantomblandzero"}

        for raw in raw_games:
            profile = self.map_game_profile(raw)
            dom = profile["gameDomain"]
            if dom.lower() in IGNORED_COMMON_APPS:
                continue

            # 核心游戏处理
            if dom in core_targets and profile["exists"]:
                if dom == "phantomblandzero":
                    try:
                        Path(profile["modsPath"]).mkdir(parents=True, exist_ok=True)
                        profile["mods_dir_exists"] = True
                    except Exception:
                        pass
                if dom in active:
                    old_p = Path(active[dom]["gamePath"])
                    new_p = Path(profile["gamePath"])
                    old_has_exe = any(old_p.glob("*.exe")) or any(old_p.glob("*/*.exe")) or any(old_p.glob("*/*/*.exe"))
                    new_has_exe = any(new_p.glob("*.exe")) or any(new_p.glob("*/*.exe")) or any(new_p.glob("*/*/*.exe"))
                    if not old_has_exe and new_has_exe:
                        active[dom] = profile
                else:
                    active[dom] = profile
                continue

            # 其他常规游戏：仅当模组目录切实存在且包含实际模组文件时才收录，避免空目录误报
            if only_with_mods:
                mp = Path(profile["modsPath"])
                has_actual_mods = False
                if mp.exists() and mp.is_dir():
                    try:
                        has_actual_mods = any(not f.name.startswith(".") for f in mp.iterdir())
                    except Exception:
                        pass
                if has_actual_mods:
                    active[dom] = profile
            else:
                active[dom] = profile

        return {"active_games": active}

    def generate_config(self, base_cfg: Optional[dict] = None) -> dict:
        cfg = base_cfg or {}
        existing_games = cfg.get("games", {})

        # 执行精简扫描
        detected = self.detect_all(only_with_mods=True)
        active = detected["active_games"]

        default_game = "stardewvalley"
        if "stardewvalley" in active:
            default_game = "stardewvalley"
        elif active:
            default_game = list(active.keys())[0]

        # 仅保留真实活跃的游戏条目，清理多余项
        games_dict = {}
        for dom, info in active.items():
            games_dict[dom] = {
                "name": info["name"],
                "gamePath": info["gamePath"],
                "modsPath": info["modsPath"],
                "gameDomain": info["gameDomain"],
                "status": "installed_active",
                "mods_dir_exists": info["mods_dir_exists"],
                "source": info.get("source")
            }

        out = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "defaultGame": default_game,
            "nexusApiKey": cfg.get("nexusApiKey", ""),
            "tmpFolderPath": cfg.get("tmpFolderPath", "./tmp_downloads"),
            "detected_summary": {
                "active_installed_count": len(games_dict),
                "primary_active_game": default_game,
                "stardew_valley_installed": "stardewvalley" in games_dict,
                "stardew_mods_path": games_dict.get("stardewvalley", {}).get("modsPath")
            },
            "games": games_dict
        }
        if isinstance(cfg.get("environment"), dict):
            out["environment"] = dict(cfg["environment"])
        return out


def ensure_game_in_config(config_path: Path, game_query: str, custom_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    【按需添加核心】当用户指定要给某个游戏装 mod 时触发：
    1. 若该游戏已在 mod_config.json 中且有效，直接返回；
    2. 若不在，自动定向扫描探测该游戏的真实安装目录与 Mods 目录；
    3. 找到后追加写入 mod_config.json，并初始化清单分文件；
    4. 若未找到且未传自定义路径，返回 None。
    """
    clean_q = game_query.strip().lower()
    domain = GAME_ALIAS_MAP.get(clean_q, clean_q)

    cfg = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

    games = cfg.setdefault("games", {})

    # 1. 如果已存在该游戏配置且本地游戏目录存在
    if domain in games:
        ginfo = games[domain]
        if Path(ginfo.get("gamePath", "")).exists():
            return ginfo

    # 2. 如果传入了自定义路径
    if custom_path:
        gp = Path(custom_path).resolve()
        if gp.exists():
            # 推测 mods 目录
            mods_sub = "Mods"
            for aid, (d, ms, dn, nid) in STEAM_APPID_MAP.items():
                if d == domain:
                    mods_sub = ms
                    break
            mp = gp / mods_sub
            new_entry = {
                "name": domain.title(),
                "gamePath": str(gp).replace("\\", "/"),
                "modsPath": str(mp).replace("\\", "/"),
                "gameDomain": domain,
                "status": "installed_active",
                "mods_dir_exists": mp.exists(),
                "source": "CustomSpecified"
            }
            games[domain] = new_entry
            cfg["detected_summary"]["active_installed_count"] = len(games)
            config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[配置动态添加] 已成功将游戏 [{domain}] 追加至: {config_path}")
            return new_entry

    # 3. 自动定向探测该游戏
    detector = GameDetector()
    detected_profile = detector.detect_single_game(game_query)
    if detected_profile and detected_profile["exists"]:
        new_entry = {
            "name": detected_profile["name"],
            "gamePath": detected_profile["gamePath"],
            "modsPath": detected_profile["modsPath"],
            "gameDomain": detected_profile["gameDomain"],
            "status": "installed_active",
            "mods_dir_exists": detected_profile["mods_dir_exists"],
            "source": detected_profile.get("source") or "OnDemandDetected"
        }
        domain_key = detected_profile["gameDomain"]
        games[domain_key] = new_entry
        cfg.setdefault("detected_summary", {})["active_installed_count"] = len(games)
        config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n[按需探测成功] 成功扫描到游戏 [{new_entry['name']}] 并已动态写入 mod_config.json！")
        print(f"  - 游戏目录: {new_entry['gamePath']}")
        print(f"  - 模组目录: {new_entry['modsPath']}")
        return new_entry

    return None


def init_mod_config(target_path: Path, force: bool = False) -> dict:
    """严格尊重已有游戏白名单的配置读取与更新，绝不擅自全盘扫描添加已被用户删除的游戏"""
    existing = {}
    if target_path.exists():
        try:
            existing = json.loads(target_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    api_key = existing.get("nexusApiKey", "")
    if isinstance(api_key, str):
        api_key = api_key.strip().strip('"').strip("'")
    if not api_key:
        api_key = os.environ.get("NEXUS_API_KEY", "")

    from config_manager import init_project_environment

    # 若配置文件已存在且已经配置了 games，除非显式指定 force，否则保留白名单并补全初始化环境与 MCP 配置
    if existing.get("games") and not force:
        existing["nexusApiKey"] = api_key
        return init_project_environment(existing, project_root=target_path.parent, save_mcp=True, save_cfg=True)

    detector = GameDetector()
    new_cfg = detector.generate_config(existing)
    new_cfg["nexusApiKey"] = api_key
    new_cfg = init_project_environment(new_cfg, project_root=target_path.parent, save_mcp=True, save_cfg=False)

    # 扫描所有活跃游戏的模组清单并写入 installed_mods/<domain>.json
    try:
        from adaptive_installer import AdaptiveInstallEngine
        engine = AdaptiveInstallEngine()

        for dom, ginfo in new_cfg.get("games", {}).items():
            if ginfo.get("status") == "installed_active":
                try:
                    mpath = Path(ginfo["modsPath"])
                    if not mpath.exists() and dom in ("phantomblandzero", "cyberpunk2077"):
                        try:
                            mpath.mkdir(parents=True, exist_ok=True)
                            ginfo["mods_dir_exists"] = True
                        except Exception:
                            pass

                    scanned = engine.scan_installed_mods(mpath, dom)
                    ginfo["installed_mods_count"] = len(scanned)
                    reg_file = engine.get_installed_registry_path(mpath, game_domain=dom)
                    try:
                        rel_reg = reg_file.resolve().relative_to(target_path.parent.resolve())
                        ginfo["installed_mods_file"] = f"./{rel_reg.as_posix()}"
                    except Exception:
                        ginfo["installed_mods_file"] = str(reg_file).replace("\\", "/")
                except Exception as ge:
                    print(f"[提示] 扫描游戏 [{dom}] 提示: {ge}")

        def_dom = new_cfg.get("defaultGame", "stardewvalley")
        workspace_reg_file = target_path.parent / "installed_mods.json"
        grouped_all = engine.get_full_grouped_registry()
        workspace_reg_file.write_text(json.dumps(grouped_all, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            rel_ws = workspace_reg_file.resolve().relative_to(target_path.parent.resolve())
            new_cfg["detected_summary"]["workspace_installed_mods_file"] = f"./{rel_ws.as_posix()}"
        except Exception:
            new_cfg["detected_summary"]["workspace_installed_mods_file"] = str(workspace_reg_file).replace("\\", "/")
        new_cfg["detected_summary"]["primary_game_mods_count"] = len(grouped_all.get(def_dom, {}))
    except Exception as e:
        print(f"[警告] 模组清单扫描提示: {e}")

    new_cfg = init_project_environment(new_cfg, project_root=target_path.parent, save_mcp=True, save_cfg=False)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(new_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return new_cfg


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="本地游戏探测与按需配置工具")
    parser.add_argument("--detect-game", help="按需探测指定游戏并返回其路径")
    parser.add_argument("--add-game", help="按需探测并将指定游戏添加至 mod_config.json")
    parser.add_argument("--game-path", help="配合 --add-game 指定的手动游戏根目录")
    parser.add_argument("--force", action="store_true", help="强制重新扫描游戏并覆盖白名单")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[4]
    if not (project_root / ".agents").exists():
        from config_manager import find_project_root
        project_root = find_project_root()
    config_file = project_root / "mod_config.json"

    if args.add_game:
        res = ensure_game_in_config(config_file, args.add_game, custom_path=args.game_path)
        if res:
            print("添加成功:", json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"[未找到] 本地未自动探测到游戏 [{args.add_game}]，请使用 --game-path 手动指定其安装根目录。")
    elif args.detect_game:
        det = GameDetector()
        res = det.detect_single_game(args.detect_game)
        print("探测结果:", json.dumps(res, indent=2, ensure_ascii=False) if res else "未找到")
    else:
        cfg = init_mod_config(config_file, force=args.force)
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
