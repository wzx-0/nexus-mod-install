"""
adaptive_installer.py — 自适应游戏模组管理、多格式扫描与安装规则学习引擎

功能特性：
  1. 识别各游戏的模组格式（SMAPI、LML/RDR2、UE4/UE5/Pak、通用松散文件）
  2. 初始化扫描：支持带 manifest.json (星露谷) 以及无 manifest 游戏 (荒野大镖客2、影之刃零等)
  3. 智能关联与清单管理：自动在 Mods 目录下生成或更新 installed_mods.json 模组清单
     （包含本地名称、文件夹、版本、匹配到的 Nexus Mod ID 等）。
     初始化时若无 Nexus ID，会尝试通过名称反向检索 Nexus；找不到则标记为 null。
  4. 多游戏自适应安装与规则记忆 (game_install_rules.json)
  5. 递归前置依赖解析 (manifest.json Dependencies) 与版本校验
  6. 汉化翻译包智能合并覆盖至 i18n 目录，严格保留用户配置 (config.json)
"""

import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# 导入集中化映射与规则配置
from config_manager import (
    load_mappings, get_game_profiles, get_mod_translations,
    get_ue4ss_builtin_folders, get_game_mod_metadata
)

try:
    import py7zr
    HAS_PY7ZR = True
except ImportError:
    HAS_PY7ZR = False


def find_project_root(start_path: Optional[Path] = None) -> Path:
    """动态定位项目根目录（寻找包含 mod_config.json 或 .agents 的目录）"""
    if start_path is None:
        start_path = Path.cwd()
    curr = start_path.resolve()
    for p in [curr] + list(curr.parents):
        if (p / "mod_config.json").exists() or (p / ".agents").exists():
            return p
    # 通过脚本文件相对层级回溯
    script_root = Path(__file__).resolve().parents[4]
    if script_root.exists() and (script_root / ".agents").exists():
        return script_root
    return curr


PROJECT_ROOT = find_project_root()
LOCAL_RULES_FILE = PROJECT_ROOT / "game_install_rules.json"

# ==============================================================================
# 游戏规则 Profiles 与模组中文对照字典 (已完全抽离至 mod_mappings.json)
# ==============================================================================
DEFAULT_GAME_PROFILES = get_game_profiles()


class _DynamicTranslationDict(dict):
    """动态中文字典代理类，对齐外部 mod_mappings.json，修改 JSON 立即热生效"""
    def __getitem__(self, key):
        return get_mod_translations("stardewvalley").get("mod_names", {}).get(key)
    def __contains__(self, key):
        return key in get_mod_translations("stardewvalley").get("mod_names", {})
    def get(self, key, default=None):
        return get_mod_translations("stardewvalley").get("mod_names", {}).get(key, default)
    def items(self):
        return get_mod_translations("stardewvalley").get("mod_names", {}).items()


STARDEW_MOD_CHINESE_NAMES = _DynamicTranslationDict()


def get_stardew_chinese_name(name: str, folder: str = "", unique_id: str = "", manifest_data: dict = None) -> str:
    """智能解析星露谷模组的中文译名 (完全基于外部 mod_mappings.json 字典配置)"""
    stardew_trans = get_mod_translations("stardewvalley")
    name_map = stardew_trans.get("mod_names", {})
    npc_zh = stardew_trans.get("npc_names", {})
    keywords = stardew_trans.get("keywords", {})

    # 1. 优先从静态对照表匹配
    for key in (name, folder, unique_id):
        if key and key in name_map:
            return name_map[key]

    # 2. 若原生名称已包含中文字符，直接返回
    if any('一' <= char <= '鿿' for char in str(name)):
        return name

    # 3. 常见 NPC 美化命名智能模式匹配
    low_name = name.lower()
    for npc_en, npc_cn in npc_zh.items():
        if npc_en in low_name:
            if any(k in low_name for k in ("character", "portrait", "sprite", "avatar")):
                prefix = "[CP] " if name.strip().startswith("[CP]") else ""
                return f"{prefix}{npc_cn}角色与肖像美化"

    # 4. 常见通用功能词推导
    prefix = ""
    clean_name = name
    for p in ["[CP] ", "[FTM] ", "[CC] ", "[JA] "]:
        if clean_name.startswith(p):
            prefix = p
            clean_name = clean_name[len(p):].strip()

    for kw_en, kw_cn in keywords.items():
        if kw_en in clean_name.lower():
            return f"{prefix}{clean_name} ({kw_cn})"

    return name


def _load_json_flexible(text: str) -> dict:
    """灵活解析 JSON：自动去除 C 风格多行注释与单行注释"""
    t = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    t = re.sub(r"(?<!:)//.*$", "", t, flags=re.MULTILINE)
    return json.loads(t)


def _is_mod_entry(item: Any) -> bool:
    """判断字典是否为单个模组的属性对象，而非游戏分组容器"""
    return isinstance(item, dict) and any(k in item for k in ("name", "unique_id", "version", "status", "format", "nexus_id"))


class GroupedRegistry(dict):
    """
    按游戏分组的已安装模组清单字典类：
    顶层为各游戏 Domain (如 "stardewvalley", "reddeadredemption2")，
    同时重载 __getitem__ / __contains__ / get 方法，向下兼容直接使用 mod_key 访问。
    """
    def __init__(self, data: dict, current_game: Optional[str] = None):
        super().__init__(data)
        self.current_game = current_game

    def get_game_mods(self, game_domain: str) -> dict:
        return self.get(game_domain, {})

    def __getitem__(self, key):
        if key in self:
            return super().__getitem__(key)
        if self.current_game and self.current_game in self:
            gmods = super().get(self.current_game, {})
            if isinstance(gmods, dict) and key in gmods:
                return gmods[key]
        for gname, gmods in self.items():
            if isinstance(gmods, dict) and key in gmods:
                return gmods[key]
        raise KeyError(key)

    def __contains__(self, key):
        if super().__contains__(key):
            return True
        if self.current_game and self.current_game in self:
            gmods = super().get(self.current_game, {})
            if isinstance(gmods, dict) and key in gmods:
                return True
        for gname, gmods in self.items():
            if isinstance(gmods, dict) and key in gmods:
                return True
        return False

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class AdaptiveInstallEngine:
    def __init__(self, custom_rules_path: Optional[Path] = None, registry_dir: Optional[Path] = None):
        self.rules_path = custom_rules_path or LOCAL_RULES_FILE
        self.rules: Dict[str, Any] = self._load_rules()
        self.registry_dir = registry_dir or PROJECT_ROOT

    def _load_rules(self) -> Dict[str, Any]:
        merged = dict(get_game_profiles())
        if self.rules_path.exists():
            try:
                learned = json.loads(self.rules_path.read_text(encoding="utf-8"))
                merged.update(learned)
            except Exception as e:
                print(f"[警告] 读取规则文件失败: {e}")
        return merged

    def save_learned_rule(self, game_domain: str, rule_data: dict) -> None:
        self.rules[game_domain] = rule_data
        try:
            self.rules_path.parent.mkdir(parents=True, exist_ok=True)
            self.rules_path.write_text(json.dumps(self.rules, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _infer_game_domain(self, mods_path: Optional[Path]) -> str:
        if mods_path:
            mp = str(mods_path).lower()
            if "stardew" in mp:
                return "stardewvalley"
            if "red dead" in mp or "rdr2" in mp:
                return "reddeadredemption2"
            if "cyberpunk" in mp:
                return "cyberpunk2077"
            if "skyrim" in mp:
                return "skyrimspecialedition"
            if "phantom" in mp or "pbz" in mp:
                return "phantomblandzero"
            if "wukong" in mp or "b1" in mp:
                return "blackmythwukong"
        return "stardewvalley"

    def get_installed_registry_path(self, mods_path: Optional[Path] = None, game_domain: Optional[str] = None) -> Path:
        """
        获取当前已安装模组清单分文件路径：
        严格遵循 mod_config.json (含自定义 MOD_CONFIG_PATH) 中的 installedModsDir 配置。
        """
        target_game = game_domain or self._infer_game_domain(mods_path)
        if self.registry_dir != PROJECT_ROOT:
            return self.registry_dir / "installed_mods" / f"{target_game}.json"

        try:
            from config_manager import get_system_paths, load_config
            paths = get_system_paths(load_config())
            return paths["installed_mods_dir"] / f"{target_game}.json"
        except Exception:
            return self.registry_dir / "installed_mods" / f"{target_game}.json"

    def load_installed_registry(self, mods_path: Optional[Path] = None, game_domain: Optional[str] = None) -> Dict[str, Any]:
        """读取指定游戏的分文件清单 (installed_mods/<game_domain>.json)"""
        target_game = game_domain or self._infer_game_domain(mods_path)
        reg_file = self.get_installed_registry_path(mods_path, target_game)
        if reg_file.exists():
            try:
                raw = json.loads(reg_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    if target_game in raw and isinstance(raw[target_game], dict) and not _is_mod_entry(raw[target_game]):
                        return raw[target_game]
                    return raw
            except Exception:
                pass

        # 兼容回退：如果分文件尚未建立，尝试从旧版 installed_mods.json 读取并自动提取对应游戏
        test_dir = (mods_path if mods_path.is_dir() else mods_path.parent) if mods_path and ("tmp" in str(mods_path).lower() or "temp" in str(mods_path).lower()) else self.registry_dir
        legacy_file = test_dir / "installed_mods.json"
        if legacy_file.exists():
            try:
                raw = json.loads(legacy_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    if target_game in raw and isinstance(raw[target_game], dict) and not _is_mod_entry(raw[target_game]):
                        return raw[target_game]
                    if any(_is_mod_entry(v) for v in raw.values()):
                        return raw
            except Exception:
                pass
        return {}

    def save_installed_registry(self, mods_path: Optional[Path] = None, data: Dict[str, Any] = None, game_domain: Optional[str] = None) -> None:
        """保存已安装模组清单：按游戏分组分文件持久化至 installed_mods/<game_domain>.json"""
        target_game = game_domain or self._infer_game_domain(mods_path)
        reg_file = self.get_installed_registry_path(mods_path, target_game)
        try:
            reg_file.parent.mkdir(parents=True, exist_ok=True)
            # 若 data 含有游戏外壳，剥离提取该游戏的内部字典
            if data and target_game in data and isinstance(data[target_game], dict) and not _is_mod_entry(data[target_game]):
                mod_data = data[target_game]
            else:
                mod_data = data or {}
            reg_file.write_text(json.dumps(mod_data, indent=2, ensure_ascii=False), encoding="utf-8")

            # 同时在清单目录父级维护一个向后兼容的汇总 installed_mods.json（绝不写入游戏 Mods 目录内）
            try:
                legacy_root = reg_file.parent.parent
                legacy_file = legacy_root / "installed_mods.json"
                grouped = self.get_full_grouped_registry(mods_path)
                grouped[target_game] = mod_data
                legacy_file.write_text(json.dumps(grouped, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        except Exception as e:
            print(f"[警告] 保存已安装模组分文件清单失败: {e}")

    def get_full_grouped_registry(self, mods_path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
        """获取所有游戏的模组汇总字典（聚合 installed_mods/ 目录下各游戏的分文件）"""
        res = {}
        target_dir = self.registry_dir
        if mods_path:
            mp_str = str(mods_path).lower()
            if "tmp" in mp_str or "temp" in mp_str or "mod_test_" in mp_str or "proj_test_" in mp_str or "uninstall_test_" in mp_str:
                target_dir = mods_path if mods_path.is_dir() else mods_path.parent

        # 读取已授权游戏白名单（严格以 mod_config.json 为准）
        allowed_games = set()
        cfg_file = target_dir / "mod_config.json"
        if cfg_file.exists():
            try:
                cfg_data = json.loads(cfg_file.read_text(encoding="utf-8"))
                allowed_games = set(cfg_data.get("games", {}).keys())
            except Exception:
                pass

        im_dir = target_dir / "installed_mods"
        if im_dir.exists() and im_dir.is_dir():
            for jf in im_dir.glob("*.json"):
                gdomain = jf.stem
                if allowed_games and gdomain not in allowed_games:
                    continue  # 坚决不读取未授权游戏，彻底杜绝加回
                try:
                    data = json.loads(jf.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        res[gdomain] = data
                except Exception:
                    pass

        # 若分文件目录为空，尝试从汇总 index 文件加载
        if not res:
            legacy_file = target_dir / "installed_mods.json"
            if legacy_file.exists():
                try:
                    raw = json.loads(legacy_file.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        has_grouping = any(isinstance(v, dict) and not _is_mod_entry(v) for v in raw.values())
                        if has_grouping:
                            res = raw
                        else:
                            res = {"stardewvalley": raw}
                except Exception:
                    pass
        return res


    def _scan_cyberpunk_mods(self, mods_path: Path, registry: dict) -> List[Dict[str, Any]]:
        """
        赛博朋克2077 逻辑模组多文件深度聚合扫描：
        自动将散落于 archive/pc/mod, bin/.../cyber_engine_tweaks, red4ext, r6/scripts, mods 的关联组件归集为一个完整逻辑模组。
        """
        if mods_path.name == "mod":
            cp_root = mods_path.parents[2]
        elif (mods_path / "bin").exists():
            cp_root = mods_path
        else:
            cp_root = mods_path.parent

        archive_dir = cp_root / "archive" / "pc" / "mod"
        cet_dir = cp_root / "bin" / "x64" / "plugins" / "cyber_engine_tweaks" / "mods"
        red4_dir = cp_root / "red4ext" / "plugins"
        rs_dir = cp_root / "r6" / "scripts"
        rm_dir = cp_root / "mods"

        archive_files = [f.name for f in archive_dir.glob("*")] if archive_dir.exists() else []
        cet_mods = [f.name for f in cet_dir.iterdir() if f.is_dir() and not f.name.startswith(".")] if cet_dir.exists() else []
        red4ext_plugins = [f.name for f in red4_dir.iterdir() if f.is_dir() and not f.name.startswith(".")] if red4_dir.exists() else []
        redscript_items = [f.name for f in rs_dir.iterdir() if not f.name.startswith(".")] if rs_dir.exists() else []
        redmod_items = [f.name for f in rm_dir.iterdir() if f.is_dir() and not f.name.startswith(".")] if rm_dir.exists() else []

        grouped = {}
        def get_or_create(key, name, category, default_format="composite_mod"):
            if key not in grouped:
                grouped[key] = {
                    "name": name,
                    "category": category,
                    "format": default_format,
                    "files": []
                }
            return grouped[key]

        # 1. RED4ext 核心框架
        for r in red4ext_plugins:
            m = get_or_create(r.lower(), r, "Core Framework", "red4ext_plugin")
            m["files"].append({"type": "red4ext_plugin", "path": f"red4ext/plugins/{r}"})

        # 2. CET 插件
        for c in cet_mods:
            c_clean = c.lower().replace(" ", "").replace("_", "").replace("-", "")
            if "hotscenes" in c_clean:
                key, name, cat = "hotscenes", "Hot Scenes (剧情与NPC动画热点)", "Animation & Scene"
            elif "sp0" in c_clean or "bodymod" in c_clean:
                key, name, cat = "sp0_bodymod", "SP0 Body Mod & Armor Adjuster (身形模组与装备适配器)", "Body & Appearance"
            elif "nativesettings" in c_clean:
                key, name, cat = "nativesettings", "Native Settings UI (原生设置菜单框架)", "Core Framework"
            elif "silentsilencer" in c_clean:
                key, name, cat = "silentsilencers", "Silent Silencers and Knives (消音器与飞刀消音)", "Gameplay & Weapons"
            elif "appearancechangeunlocker" in c_clean:
                key, name, cat = "appearancechangeunlocker", "Appearance Change Unlocker (捏脸/外观更换解锁)", "Appearance & Character"
            elif "npcbodytweaks" in c_clean or "cetnpcbody" in c_clean:
                key, name, cat = "cet_npc_body_tweaks", "CET NPC Body Tweaks (NPC身材微调)", "NPC & Appearance"
            elif "carmodshop" in c_clean:
                key, name, cat = "carmodshop", "Car Mod Shop (载具改装商店)", "Vehicle & Shop"
            elif "nightvision" in c_clean:
                key, name, cat = "nightvision", "Night Vision (夜视仪)", "Gameplay & Equipment"
            elif "stocks" in c_clean:
                key, name, cat = "stocks", "Stock Market (股票交易系统)", "Gameplay & Economy"
            elif "autoloot" in c_clean:
                key, name, cat = "autoloot", "AutoLoot (自动拾取)", "Gameplay & Utility"
            elif "autoammo" in c_clean or "morebullets" in c_clean:
                key, name, cat = "autoammocrafting", "Auto Ammo Crafting (自动弹药制造)", "Gameplay & Utility"
            elif "junkscrapper" in c_clean:
                key, name, cat = "automatic_junk_scrapper", "Automatic Junk Scrapper Perk (自动废品拆解增强)", "Gameplay & Utility"
            elif "immersivefirstperson" in c_clean:
                key, name, cat = "immersivefirstperson", "Immersive First Person (沉浸式第一人称身体视角)", "Camera & Immersion"
            elif "edgerunner" in c_clean or "nocwcost" in c_clean:
                key, name, cat = "no_cw_cost_edgerunner", "No CW Cost Edgerunner (义体零消耗边缘行者)", "Gameplay & Balance"
            elif "questtracking" in c_clean:
                key, name, cat = "questtrackingtoggle", "Quest Tracking Toggle (任务追踪显示切换)", "UI & HUD"
            elif "vehicledurability" in c_clean:
                key, name, cat = "vehicledurabilitydisplay", "Vehicle Durability Display (载具耐久度显示)", "Vehicle & UI"
            elif "fasterelevators" in c_clean:
                key, name, cat = "faster_elevators", "Faster Elevators (快速电梯)", "Gameplay & Quality of Life"
            elif "laptop" in c_clean:
                key, name, cat = "laptop", "Laptop Interaction (便携笔记本电脑)", "Gameplay & Immersion"
            else:
                key, name, cat = c.lower(), c, "CET Script"
            m = get_or_create(key, name, cat, "cet_mod")
            m["files"].append({"type": "cet_mod", "path": f"bin/x64/plugins/cyber_engine_tweaks/mods/{c}"})

        # 3. r6/scripts
        for s in redscript_items:
            s_clean = s.lower().replace(" ", "").replace("_", "").replace("-", "").replace(".reds", "")
            if "equipmentex" in s_clean:
                key, name, cat = "equipmentex", "EquipmentEx (衣柜穿搭扩展系统)", "Core & Clothing"
            elif "appearancechangeunlocker" in s_clean:
                key, name, cat = "appearancechangeunlocker", "Appearance Change Unlocker (捏脸/外观更换解锁)", "Appearance & Character"
            elif "autodrive" in s_clean:
                key, name, cat = "autodrive", "Auto Drive (自动寻路驾驶)", "Vehicle & Navigation"
            elif "quickhack" in s_clean:
                key, name, cat = "quickhack_fixes", "Quickhack Fixes & Sort (快速破解修复与槽位排序)", "Gameplay & Fixes"
            elif "silentsilencer" in s_clean or "unlimitedknife" in s_clean:
                key, name, cat = "silentsilencers", "Silent Silencers and Knives (消音器与飞刀消音)", "Gameplay & Weapons"
            elif "virtualatelier" in s_clean:
                key, name, cat = "virtualatelier", "Virtual Atelier (虚拟服装工作室商店框架)", "Shop & Clothing Framework"
            elif "scorpion" in s_clean:
                key, name, cat = "scorpion_military", "Scorpion Military Outfit & Gear (天蝎军事装备及虚拟商店)", "Clothing & Gear"
            elif "npcbodytweaks" in s_clean or "cetnpcbody" in s_clean:
                key, name, cat = "cet_npc_body_tweaks", "CET NPC Body Tweaks (NPC身材微调)", "NPC & Appearance"
            elif "cyberwareex" in s_clean:
                key, name, cat = "cyberwareex", "CyberwareEx (义体插槽容量扩展)", "Gameplay & Cyberware"
            elif "fasttravel" in s_clean:
                key, name, cat = "fasttravel_anywhere", "Fast Travel From Anywhere (全地图任意位置快速传送)", "Quality of Life & Travel"
            elif "resetattributes" in s_clean:
                key, name, cat = "reset_attributes", "Reset Attributes Always Available (属性点随时重置)", "Quality of Life & Character"
            elif "statusbar" in s_clean:
                key, name, cat = "statusbar_fixes", "Status Bar Bug Fixes (状态栏 UI 修复)", "UI & Bug Fixes"
            elif "overclock" in s_clean:
                key, name, cat = "overclock_health_drain_fix", "Overclock Health Drain Fix (超频生命流失修复)", "Gameplay & Fixes"
            elif "realvendor" in s_clean:
                key, name, cat = "real_vendor_names", "Real Vendor Names (真实商人名称显示)", "UI & Immersion"
            elif "stashfilter" in s_clean:
                key, name, cat = "stash_filters", "Stash Filters (储藏箱物品筛选器)", "UI & Quality of Life"
            else:
                key = s.lower().replace(".reds", "")
                name = s
                cat = "Redscript"
            m = get_or_create(key, name, cat, "redscript_mod")
            m["files"].append({"type": "redscript", "path": f"r6/scripts/{s}"})

        # 4. archive/pc/mod
        for a in archive_files:
            if a.endswith(".gmmback"):
                continue
            a_clean = a.lower().replace(" ", "").replace("_", "").replace("-", "")
            if "equipmentex" in a_clean:
                key, name, cat = "equipmentex", "EquipmentEx (衣柜穿搭扩展系统)", "Core & Clothing"
            elif "appearancechangeunlocker" in a_clean:
                key, name, cat = "appearancechangeunlocker", "Appearance Change Unlocker (捏脸/外观更换解锁)", "Appearance & Character"
            elif "autodrive" in a_clean:
                key, name, cat = "autodrive", "Auto Drive (自动寻路驾驶)", "Vehicle & Navigation"
            elif "quickhack" in a_clean:
                key, name, cat = "quickhack_fixes", "Quickhack Fixes & Sort (快速破解修复与槽位排序)", "Gameplay & Fixes"
            elif "silentsilencer" in a_clean:
                key, name, cat = "silentsilencers", "Silent Silencers and Knives (消音器与飞刀消音)", "Gameplay & Weapons"
            elif "virtualatelier" in a_clean:
                key, name, cat = "virtualatelier", "Virtual Atelier (虚拟服装工作室商店框架)", "Shop & Clothing Framework"
            elif "scorpion" in a_clean:
                key, name, cat = "scorpion_military", "Scorpion Military Outfit & Gear (天蝎军事装备及虚拟商店)", "Clothing & Gear"
            elif "sp0" in a_clean or "bodymod" in a_clean:
                key, name, cat = "sp0_bodymod", "SP0 Body Mod & Armor Adjuster (身形模组与装备适配器)", "Body & Appearance"
            elif "hotscenes" in a_clean:
                key, name, cat = "hotscenes", "Hot Scenes (剧情与NPC动画热点)", "Animation & Scene"
            elif "cetbasebodies" in a_clean or "npcbodytweaks" in a_clean:
                key, name, cat = "cet_npc_body_tweaks", "CET NPC Body Tweaks (NPC身材微调)", "NPC & Appearance"
            elif "cmsgarage" in a_clean:
                key, name, cat = "carmodshop", "Car Mod Shop (载具改装商店)", "Vehicle & Shop"
            elif "nightvision" in a_clean:
                key, name, cat = "nightvision", "Night Vision (夜视仪)", "Gameplay & Equipment"
            elif "stockicon" in a_clean:
                key, name, cat = "stocks", "Stock Market (股票交易系统)", "Gameplay & Economy"
            elif "xrxstock" in a_clean:
                key, name, cat = "xrx_stockings", "XRX Stockings 2024 (ArchiveXL 丝袜包)", "Clothing"
            elif "peachu" in a_clean or "espressoboots" in a_clean:
                key, name, cat = "peachu_espresso_boots", "Peachu Espresso Boots (浓缩咖啡长靴 ArchiveXL)", "Clothing"
            elif "breastjiggle" in a_clean:
                key, name, cat = "breast_jiggle_physics", "Breast Jiggle Physics (女性身形物理晃动效果)", "Body & Physics"
            elif "collapsedneck" in a_clean:
                key, name, cat = "fpp_clothing_collapsed_neck_fix", "FPP Clothing Collapsed Neck Fix (第一人称衣领塌陷修复)", "Bug Fixes"
            elif "nointrovideos" in a_clean:
                key, name, cat = "basegame_no_intro_videos", "No Intro Videos (跳过启动LOGO与开场视频)", "Quality of Life"
            else:
                clean_stem = Path(a).stem
                key, name, cat = clean_stem.lower(), clean_stem, "Archive Mod"
            m = get_or_create(key, name, cat, "archive_mod")
            m["files"].append({"type": "archive", "path": f"archive/pc/mod/{a}"})

        # 5. REDmod
        for rm in redmod_items:
            if rm == ".stub":
                continue
            m = get_or_create(rm.lower(), rm, "REDmod Package", "redmod")
            m["files"].append({"type": "redmod", "path": f"mods/{rm}"})

        scanned = []
        for key, info in sorted(grouped.items(), key=lambda x: x[0]):
            file_count = len(info["files"])
            primary_rel = info["files"][0]["path"] if info["files"] else f"archive/pc/mod/{key}"
            primary_path = cp_root / primary_rel
            format_name = "composite_mod" if file_count > 1 else info["format"]
            scanned.append({
                "name": info["name"],
                "unique_id": f"cp77.{key}",
                "version": registry.get(key, {}).get("version", "未知"),
                "folder": key,
                "path": str(primary_path).replace("\\", "/"),
                "nexus_id": registry.get(key, {}).get("nexus_id"),
                "format": format_name,
                "category": info["category"],
                "files": info["files"],
                "dependencies": []
            })
        return scanned

    def _scan_pbz_mods(self, mods_path: Path, registry: dict) -> List[Dict[str, Any]]:
        """
        影之刃零 (Phantom Blade Zero) UE4SS 模组扫描引擎：
        探测 platform/steam/ue4ss/Mods 目录，读取 mods.txt / mods.json 激活状态，
        识别核心用户模组（PbzModConfig 模组配置页面、PbzMapTracker 地图资源追踪）及 UE4SS 框架插件。
        """
        pbz_root = None
        if (mods_path / "mods.txt").exists() or (mods_path / "PbzModConfig").exists():
            ue4ss_mods = mods_path
            pbz_root = mods_path.parents[3] if len(mods_path.parents) >= 4 else mods_path.parent
        elif (mods_path.parent / "platform" / "steam" / "ue4ss" / "Mods").exists():
            ue4ss_mods = mods_path.parent / "platform" / "steam" / "ue4ss" / "Mods"
            pbz_root = mods_path.parent
        else:
            candidate = None
            for p in [mods_path] + list(mods_path.parents):
                c = p / "platform" / "steam" / "ue4ss" / "Mods"
                if c.exists():
                    candidate = c
                    pbz_root = p
                    break
            ue4ss_mods = candidate if candidate else mods_path
            if not pbz_root:
                pbz_root = mods_path.parents[3] if len(mods_path.parents) >= 4 else mods_path.parent

        enabled_map = {}
        mods_txt = ue4ss_mods / "mods.txt"
        if mods_txt.exists():
            try:
                for line in mods_txt.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line.startswith(";") or not line or ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    enabled_map[k.strip()] = (v.strip() == "1")
            except Exception:
                pass
        mods_json = ue4ss_mods / "mods.json"
        if mods_json.exists():
            try:
                mdata = json.loads(mods_json.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(mdata, list):
                    for item in mdata:
                        if isinstance(item, dict) and "mod_name" in item:
                            enabled_map[item["mod_name"]] = item.get("mod_enabled", True)
            except Exception:
                pass

        # UE4SS 内置框架模块与模组元数据：已完全抽离至 mod_mappings.json 统一管理
        ue4ss_builtin_folders = get_ue4ss_builtin_folders()

        has_ue4ss_framework = False
        ue4ss_components = []

        scanned = []
        if ue4ss_mods.exists():
            for item in sorted(ue4ss_mods.iterdir(), key=lambda x: x.name):
                if not item.is_dir() or item.name.startswith(".") or item.name.lower() in ("shared",):
                    continue
                mname = item.name
                low_name = mname.lower()

                # 如果是 UE4SS 内置组件，归集入框架，不作为独立模组展开
                if low_name in ue4ss_builtin_folders:
                    has_ue4ss_framework = True
                    ue4ss_components.append(mname)
                    continue

                # 真实独立用户模组绑定已在 mod_mappings.json 中登记的官方元数据与中文名
                is_enabled = enabled_map.get(mname, True)
                preset_meta = get_game_mod_metadata("phantomblandzero", low_name)
                if preset_meta:
                    meta = dict(preset_meta)
                else:
                    meta = {
                        "name": mname,
                        "chinese_name": mname,
                        "name_zh": mname,
                        "unique_id": f"ue4ss.{low_name}",
                        "version": registry.get(mname, {}).get("version", "1.0.0"),
                        "nexus_id": registry.get(mname, {}).get("nexus_id"),
                        "category": "UE4SS Mod",
                        "description": f"UE4SS 模组: {mname}",
                        "format": "ue4ss_lua"
                    }

                scanned.append({
                    "name": meta["name"],
                    "chinese_name": meta.get("chinese_name", meta["name"]),
                    "name_zh": meta.get("name_zh", meta["name"]),
                    "unique_id": meta["unique_id"],
                    "version": meta["version"],
                    "folder": mname,
                    "path": str(item).replace("\\", "/"),
                    "nexus_id": meta.get("nexus_id"),
                    "format": meta["format"],
                    "category": meta["category"],
                    "description": meta.get("description", ""),
                    "enabled": is_enabled,
                    "dependencies": [{"UniqueID": "UE4SS", "IsRequired": True}]
                })

            # 检测到 UE4SS 核心框架时，作为独立基础框架条目置顶展示
            if has_ue4ss_framework or (ue4ss_mods.parent / "UE4SS.dll").exists():
                scanned.insert(0, {
                    "name": "PBZ UE4SS Framework",
                    "chinese_name": "UE4SS 核心脚本框架",
                    "name_zh": "UE4SS 核心脚本框架",
                    "unique_id": "ue4ss.framework",
                    "version": "3.0.1",
                    "folder": "UE4SS",
                    "path": str(ue4ss_mods.parent).replace("\\", "/"),
                    "nexus_id": 125,
                    "format": "ue4ss_framework",
                    "category": "Core Framework",
                    "description": f"影之刃零 UE4SS 基础运行框架 (归集管理包含 {', '.join(ue4ss_components[:5])} 等内置模块)",
                    "enabled": True,
                    "dependencies": []
                })

        # 收集已有复合模组声明的附属路径，避免将附属 PAK 或补丁子目录重复拆成散碎条目
        claimed_extra_paths = set()
        for reg_item in registry.values():
            if not isinstance(reg_item, dict) or reg_item.get("status") == "uninstalled":
                continue
            for ep in (reg_item.get("extra_paths") or []) + (reg_item.get("installed_files") or []):
                claimed_extra_paths.add(str(ep).replace("\\", "/").lower())

        if pbz_root:
            paks_dir = pbz_root / "ProjectPBZ" / "Content" / "Paks" / "~mods"
            if paks_dir.exists():
                pak_groups: Dict[str, List[Path]] = {}
                for item in sorted(paks_dir.iterdir(), key=lambda x: x.name):
                    if item.is_file() and item.suffix.lower() in (".pak", ".ucas", ".utoc"):
                        norm_item = str(item).replace("\\", "/").lower()
                        rel_item = f"projectpbz/content/paks/~mods/{item.name.lower()}"
                        if norm_item in claimed_extra_paths or rel_item in claimed_extra_paths:
                            continue
                        pak_groups.setdefault(item.stem, []).append(item)

                for stem, files in pak_groups.items():
                    primary = next((f for f in files if f.suffix.lower() == ".pak"), files[0])
                    prev_entry = registry.get(stem) or registry.get(primary.name) or {}
                    zh = prev_entry.get("chinese_name") or prev_entry.get("name_zh") or ""
                    scanned.append({
                        "name": prev_entry.get("name") or stem,
                        "chinese_name": zh,
                        "name_zh": zh,
                        "unique_id": prev_entry.get("unique_id") or f"pak.{stem.lower()}",
                        "version": prev_entry.get("version", "未知"),
                        "folder": stem,
                        "path": str(primary).replace("\\", "/"),
                        "installed_files": [f"ProjectPBZ/Content/Paks/~mods/{f.name}" for f in files],
                        "nexus_id": prev_entry.get("nexus_id"),
                        "format": "ue5_pak",
                        "category": "PAK Mod",
                        "enabled": True,
                        "dependencies": []
                    })
        return scanned

    def scan_installed_mods(self, mods_path: Path, game_domain: str, api_search_func=None) -> List[Dict[str, Any]]:
        """
        初始化扫描：
          1. 星露谷：支持 manifest.json 容错解析（自动剥离注释与 BOM）与子模组识别
          2. 赛博朋克2077：跨生态扫描 archive/pc/mod, CET plugins, RED4ext, Redscript, REDmod
          3. 荒野大镖客2：扫描 lml 扩展包与游戏根目录 .asi ScriptHook 插件
          4. 影之刃零 / 虚幻5：扫描 ProjectPBZ/Content/Paks/~mods 目录下的 .pak/.ucas/.utoc
          5. 与清单缓存对比，保留历史已卸载记录，记录真实物理路径以支持精准还原
        """
        registry = self.load_installed_registry(mods_path, game_domain)

        # 离线/外接磁盘未挂载保护：若游戏根目录或所在盘符不存在，严禁将已有专属清单覆盖清空为 {}！
        from config_manager import resolve_game_root, load_config
        game_root_chk = resolve_game_root(game_domain, load_config(), mods_path)
        if not mods_path.exists():
            if not game_root_chk.exists():
                print(f"[离线保护] 游戏根目录当前未挂载或不可访问 ({game_root_chk})，保留已有专属清单记录 ({len(registry)} 项) 不予清空！")
                return [dict(v, folder=k) for k, v in registry.items() if isinstance(v, dict) and v.get("status") != "uninstalled"]
            try:
                mods_path.mkdir(parents=True, exist_ok=True)
            except Exception:
                return [dict(v, folder=k) for k, v in registry.items() if isinstance(v, dict) and v.get("status") != "uninstalled"]

        scanned_mods = []
        found_folders = set()

        # 模式 1: 星露谷物语 (Stardew Valley SMAPI)
        if game_domain == "stardewvalley":
            manifest_files = []
            wrapper_dirs = set()

            # 1. 扫描所有有效的 manifest.json 并定位外层包裹目录
            for mf in mods_path.rglob("manifest.json"):
                rel_parts = mf.relative_to(mods_path).parts
                if any(part.startswith(".") for part in rel_parts):
                    continue
                manifest_files.append((mf, rel_parts))
                # 若 manifest.json 位于嵌套子目录中，则它在 mods_path 根下的顶级目录属于包裹外壳
                if len(rel_parts) > 2:
                    wrapper_dirs.add(rel_parts[0])

            # 建立父目录与路径中的 Nexus ID 缓存（支持同包继承与从 Nexus 文件名提取）
            dir_to_nexus_id = {}
            for mf, rel_parts in manifest_files:
                for part in reversed(rel_parts):
                    m_f = re.search(r"-(\d{3,7})-", part)
                    if m_f:
                        dir_to_nexus_id[mf.parent.name] = int(m_f.group(1))
                        if len(rel_parts) > 1:
                            dir_to_nexus_id[rel_parts[0]] = int(m_f.group(1))
                        break

            # 2. 解析每一个真正的 SMAPI 模组组件
            for mf, rel_parts in manifest_files:
                try:
                    data = _load_json_flexible(mf.read_text(encoding="utf-8-sig"))
                    folder_key = mf.parent.name
                    found_folders.add(folder_key)

                    uid = data.get("UniqueID") or data.get("uniqueID") or folder_key
                    name = data.get("Name") or folder_key
                    ver = data.get("Version")
                    if isinstance(ver, dict):
                        ver = f"{ver.get('MajorVersion', 0)}.{ver.get('MinorVersion', 0)}.{ver.get('PatchVersion', 0)}"

                    # 提取 Nexus ID (优先 UpdateKeys，其次路径外壳匹配，再次同包继承，最后查已有清单缓存)
                    nexus_id = None
                    for uk in data.get("UpdateKeys") or []:
                        m = re.search(r"Nexus:\s*(\d+)", str(uk), re.I)
                        if m:
                            nexus_id = int(m.group(1))
                            break

                    if not nexus_id:
                        for part in reversed(rel_parts):
                            m_folder = re.search(r"-(\d{3,7})-", part)
                            if m_folder:
                                nexus_id = int(m_folder.group(1))
                                break

                    if not nexus_id and folder_key in dir_to_nexus_id:
                        nexus_id = dir_to_nexus_id[folder_key]
                    if not nexus_id and len(rel_parts) > 1 and rel_parts[0] in dir_to_nexus_id:
                        nexus_id = dir_to_nexus_id[rel_parts[0]]
                    if not nexus_id and folder_key in registry:
                        nexus_id = registry[folder_key].get("nexus_id")

                    # 智能获取中文翻译名
                    c_name = get_stardew_chinese_name(name, folder_key, uid, data)

                    dependencies = data.get("Dependencies") or []
                    scanned_mods.append({
                        "name": name,
                        "chinese_name": c_name,
                        "name_zh": c_name,
                        "unique_id": uid,
                        "version": str(ver or "未知"),
                        "folder": folder_key,
                        "path": str(mf.parent).replace("\\", "/"),
                        "nexus_id": nexus_id,
                        "format": "smapi_manifest",
                        "dependencies": dependencies
                    })
                except Exception:
                    pass

            # 3. 扫描根目录下的独立模组（彻底跳过外壳目录、清单目录及包含 manifest 的所有子树）
            for item in mods_path.iterdir():
                if item.name.startswith(".") or item.name in ("installed_mods.json", "installed_mods", "deleted_mods"):
                    continue
                # 如果这个 item 是外壳包裹目录或已识别模组，坚决跳过，杜绝重复统计
                if item.name in wrapper_dirs or item.name in found_folders:
                    continue
                try:
                    if any(item.rglob("manifest.json")):
                        continue
                except Exception:
                    pass
                if item.is_dir():
                    found_folders.add(item.name)
                    c_name = get_stardew_chinese_name(item.name, item.name, item.name.lower())
                    scanned_mods.append({
                        "name": item.name,
                        "chinese_name": c_name,
                        "name_zh": c_name,
                        "unique_id": item.name.lower(),
                        "version": registry.get(item.name, {}).get("version", "未知"),
                        "folder": item.name,
                        "path": str(item).replace("\\", "/"),
                        "nexus_id": registry.get(item.name, {}).get("nexus_id"),
                        "format": "folder",
                        "dependencies": []
                    })

        # 模式 2: 赛博朋克2077 (Cyberpunk 2077 — 启用多目录复合归集扫描并注入中文字典)
        elif game_domain == "cyberpunk2077":
            cp_name_map = get_mod_translations("cyberpunk2077").get("mod_names", {})
            cp_mods = self._scan_cyberpunk_mods(mods_path, registry)
            for m in cp_mods:
                fkey = m["folder"]
                zh = cp_name_map.get(fkey) or cp_name_map.get(fkey.lower()) or ""
                if zh:
                    m["chinese_name"] = zh
                    m["name_zh"] = zh
                scanned_mods.append(m)
                found_folders.add(fkey)

        # 模式 3: 荒野大镖客2 (Red Dead Redemption 2 — 扫描 LML 与 ASI 并注入中文字典)
        elif game_domain == "reddeadredemption2":
            rdr_name_map = get_mod_translations("reddeadredemption2").get("mod_names", {})
            rdr_root = mods_path if (mods_path / "RDR2.exe").exists() else mods_path.parent
            lml_dir = rdr_root / "lml"

            # 3.1 LML 模组包
            if lml_dir.exists():
                for item in lml_dir.iterdir():
                    if item.is_dir() and not item.name.startswith(".") and item.name not in ("replace", "stream"):
                        folder_key = f"lml_{item.name}"
                        found_folders.add(folder_key)
                        ver = registry.get(folder_key, {}).get("version", "未知")
                        nid = registry.get(folder_key, {}).get("nexus_id")
                        zh = rdr_name_map.get(folder_key) or rdr_name_map.get(item.name) or ""
                        scanned_mods.append({
                            "name": item.name,
                            "chinese_name": zh,
                            "name_zh": zh,
                            "unique_id": f"lml.{item.name.lower()}",
                            "version": ver,
                            "folder": folder_key,
                            "path": str(item).replace("\\", "/"),
                            "nexus_id": nid,
                            "format": "rdr2_lml",
                            "dependencies": [{"UniqueID": "LennyModLoader", "IsRequired": True}]
                        })

            # 3.2 根目录 .asi 模组
            for af in rdr_root.glob("*.asi"):
                if af.name.lower() in ("vfs.asi",):
                    continue
                folder_key = af.name
                found_folders.add(folder_key)
                nid = registry.get(folder_key, {}).get("nexus_id")
                zh = rdr_name_map.get(folder_key) or rdr_name_map.get(af.stem) or ""
                scanned_mods.append({
                    "name": af.stem,
                    "chinese_name": zh,
                    "name_zh": zh,
                    "unique_id": f"asi.{af.stem.lower()}",
                    "version": registry.get(folder_key, {}).get("version", "未知"),
                    "folder": folder_key,
                    "path": str(af).replace("\\", "/"),
                    "nexus_id": nid,
                    "format": "rdr2_scripthook_asi",
                    "dependencies": [{"UniqueID": "ScriptHookRDR2", "IsRequired": True}]
                })

        # 模式 4: 影之刃零 / 虚幻引擎5 (Phantom Blade Zero)
        elif game_domain in ("phantomblandzero", "phantombladezero"):
            pbz_mods = self._scan_pbz_mods(mods_path, registry)
            scanned_mods.extend(pbz_mods)
            for m in pbz_mods:
                found_folders.add(m["folder"])

        # 通用回退扫描
        else:
            for item in mods_path.iterdir():
                if item.name.startswith(".") or item.name == "installed_mods.json":
                    continue
                folder_key = item.name
                if folder_key in found_folders:
                    continue
                if item.is_dir():
                    found_folders.add(folder_key)
                    scanned_mods.append({
                        "name": folder_key,
                        "unique_id": folder_key.lower(),
                        "version": registry.get(folder_key, {}).get("version", "未知"),
                        "folder": folder_key,
                        "path": str(item).replace("\\", "/"),
                        "nexus_id": registry.get(folder_key, {}).get("nexus_id"),
                        "format": "folder",
                        "dependencies": []
                    })
                elif item.is_file():
                    found_folders.add(folder_key)
                    scanned_mods.append({
                        "name": item.stem,
                        "unique_id": item.stem.lower(),
                        "version": "未知",
                        "folder": folder_key,
                        "path": str(item).replace("\\", "/"),
                        "nexus_id": registry.get(folder_key, {}).get("nexus_id"),
                        "format": "loose_file",
                        "dependencies": []
                    })

        # 3. 针对未关联 Nexus ID 的模组，尝试反查 Nexus
        updated_registry = {}
        for mod in scanned_mods:
            fkey = mod["folder"]
            nid = mod.get("nexus_id")
            if not nid and api_search_func:
                try:
                    matched_id = api_search_func(mod["name"])
                    if matched_id:
                        mod["nexus_id"] = matched_id
                        print(f"  [智能关联] 模组 [{mod['name']}] 自动关联到 Nexus Mod ID: {matched_id}")
                except Exception:
                    pass

            prev = registry.get(fkey, {}) if isinstance(registry.get(fkey), dict) else {}
            zh_val = mod.get("chinese_name") or prev.get("chinese_name") or prev.get("name_zh") or ""
            ver_val = mod["version"] if mod.get("version") and mod["version"] != "未知" else prev.get("version", "未知")
            entry = {
                "name": mod["name"],
                "chinese_name": zh_val,
                "name_zh": zh_val,
                "unique_id": mod["unique_id"],
                "version": ver_val,
                "nexus_id": mod.get("nexus_id") if mod.get("nexus_id") is not None else prev.get("nexus_id"),
                "format": prev.get("format") if prev.get("format") == "ue4ss_and_pak" else mod.get("format"),
                "path": mod.get("path"),
                "dependencies": mod.get("dependencies", []),
                "status": "installed"
            }
            for keep_field in ("category", "files", "nexus_name", "nexus_url", "enabled", "description"):
                if mod.get(keep_field) is not None or prev.get(keep_field) is not None:
                    entry[keep_field] = mod.get(keep_field) if mod.get(keep_field) is not None else prev.get(keep_field)
            if mod.get("installed_files") or prev.get("installed_files"):
                entry["installed_files"] = mod.get("installed_files") or prev.get("installed_files")
            if prev.get("extra_paths"):
                entry["extra_paths"] = prev["extra_paths"]
            updated_registry[fkey] = entry

        # 4. 保留所有已标记删除/卸载的历史记录（防止被覆盖清除，便于后续快速装回）
        for old_k, old_item in registry.items():
            if old_item.get("status") == "uninstalled" and old_k not in updated_registry:
                updated_registry[old_k] = old_item

        # 保存最新的清单文件到当前项目目录 (PROJECT_ROOT)
        self.save_installed_registry(mods_path, updated_registry, game_domain)
        return scanned_mods

    @staticmethod
    def detect_archive_format(archive_path: Path) -> str:
        """通过文件头魔数 (Magic Bytes) 精准识别压缩包真实格式，防止扩展名与实际格式不符导致解压崩溃"""
        if not archive_path.exists() or not archive_path.is_file():
            raise FileNotFoundError(f"压缩包不存在: {archive_path}")

        with open(archive_path, "rb") as f:
            head = f.read(32)

        if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            return "zip"
        if head.startswith(b"7z\xbc\xaf\x27\x1c"):
            return "7z"
        if head.startswith(b"Rar!\x1a\x07"):
            return "rar"

        low_head = head.lstrip().lower()
        if low_head.startswith((b"<!doctype", b"<html", b"<?xml")):
            raise ValueError(f"文件 [{archive_path.name}] 实为网页响应而非有效压缩包，请检查下载链接是否需要授权")

        suffix = archive_path.suffix.lower()
        if suffix in (".zip", ".7z", ".rar"):
            return suffix.lstrip(".")
        return "zip"

    @staticmethod
    def _find_external_extractor() -> Optional[Tuple[str, str]]:
        """探测可用的外部解压工具（7z.exe 或系统内置 libarchive tar.exe）"""
        seven_zip_candidates = [
            shutil.which("7z"),
            shutil.which("7za"),
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
        ]
        for c in seven_zip_candidates:
            if c and Path(c).exists():
                return ("7z", str(c))

        tar_candidates = [
            r"C:\Windows\System32\tar.exe",
            shutil.which("tar"),
        ]
        for t in tar_candidates:
            if t and Path(t).exists():
                return ("tar", str(t))
        return None

    def _list_archive_files(self, archive_path: Path) -> List[str]:
        fmt = self.detect_archive_format(archive_path)
        if fmt == "zip":
            with zipfile.ZipFile(archive_path) as zf:
                return [m.filename for m in zf.infolist() if not m.is_dir()]
        if fmt == "7z" and HAS_PY7ZR:
            try:
                with py7zr.SevenZipFile(archive_path, mode="r") as sz:
                    return [n for n in sz.getnames() if not n.endswith("/")]
            except Exception:
                pass

        ext_tool = self._find_external_extractor()
        if ext_tool:
            kind, exe = ext_tool
            import subprocess
            if kind == "tar":
                proc = subprocess.run([exe, "-tf", str(archive_path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
                if proc.returncode == 0:
                    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip() and not ln.strip().endswith("/")]
            elif kind == "7z":
                proc = subprocess.run([exe, "l", "-ba", "-slt", str(archive_path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
                if proc.returncode == 0:
                    files = []
                    cur_path = None
                    is_dir = False
                    for ln in proc.stdout.splitlines():
                        if ln.startswith("Path = "):
                            cur_path = ln[7:].strip()
                        elif ln.startswith("Folder = "):
                            is_dir = (ln[9:].strip() == "+")
                        elif not ln.strip() and cur_path:
                            if not is_dir:
                                files.append(cur_path)
                            cur_path = None
                            is_dir = False
                    if cur_path and not is_dir:
                        files.append(cur_path)
                    return files
        return []

    def inspect_archive_structure(self, archive_path: Path) -> dict:
        raw_files = self._list_archive_files(archive_path)
        file_list = [f.replace("\\", "/").strip("/") for f in raw_files if f.strip("/")]

        manifest_paths = [f for f in file_list if Path(f).name.lower() == "manifest.json"]
        has_manifest_json = len(manifest_paths) > 0
        has_rdr2_files = any(f.lower().endswith((".asi", "install.xml", ".ytd", ".ydr")) or f.lower().startswith("lml/") for f in file_list)
        has_pak_files = any(f.lower().endswith((".pak", ".ucas", ".utoc")) for f in file_list)
        has_ue4ss_files = any(
            f.lower().endswith(("scripts/main.lua", "enabled.txt", "dlls/main.dll"))
            for f in file_list
        )
        has_esp_bsa = any(f.lower().endswith((".esp", ".esm", ".esl", ".bsa", ".ba2")) for f in file_list)
        has_i18n = any("i18n" in f.lower() or f.lower().endswith((".zh.json", "zh-cn.json", "chinese.json")) for f in file_list)

        cp77_roots = {"archive", "bin", "r6", "red4ext", "mods", "engine"}
        cp77_prefix = None
        for f in file_list:
            parts = f.split("/")
            if parts[0].lower() in cp77_roots:
                cp77_prefix = ""
                break
            if len(parts) > 1 and parts[1].lower() in cp77_roots:
                cp77_prefix = parts[0]

        top_items = set()
        top_dirs = set()
        for f in file_list:
            parts = f.split("/")
            top_items.add(parts[0])
            if len(parts) > 1:
                top_dirs.add(parts[0])

        is_single_root = (len(top_items) == 1 and len(top_dirs) == 1)
        single_root_name = list(top_dirs)[0] if is_single_root else None

        # 计算 SMAPI 纯空单子目录外包壳剥离前缀：
        # 仅当外层目录是“只有 1 个子目录且无任何同级文件”的纯空壳（如 ZipWrapper-1234/ModA/...）时才剥离；
        # 若某层目录包含多个子模组目录（如 Stardew Valley Expanded/[CP] SVE + [FTM] SVE），保留该整合包顶层目录。
        smapi_strip_prefix = ""
        if manifest_paths:
            all_parts = [f.split("/") for f in file_list]
            min_m_depth = min(len(mp.split("/")) for mp in manifest_paths)
            if min_m_depth >= 3:
                prefix_segs: List[str] = []
                while len(prefix_segs) < min_m_depth - 2:
                    depth = len(prefix_segs)
                    matching = [p for p in all_parts if p[:depth] == prefix_segs]
                    if any(len(p) == depth + 1 for p in matching):
                        break
                    child_dirs = {p[depth] for p in matching if len(p) > depth + 1}
                    if len(child_dirs) != 1:
                        break
                    cand_dir = next(iter(child_dirs))
                    next_segs = prefix_segs + [cand_dir]
                    inside_cand = [p for p in all_parts if p[:depth + 1] == next_segs]
                    # 检查 cand_dir 内部：只有当其内部无直接文件且仅含唯一 1 个子目录时，cand_dir 才是多余空壳
                    if any(len(p) == depth + 2 for p in inside_cand):
                        break
                    grandchild_dirs = {p[depth + 1] for p in inside_cand if len(p) > depth + 2}
                    if len(grandchild_dirs) == 1:
                        prefix_segs.append(cand_dir)
                    else:
                        break
                if prefix_segs:
                    smapi_strip_prefix = "/".join(prefix_segs)

        return {
            "total_files": len(file_list),
            "file_list": file_list,
            "manifest_paths": manifest_paths,
            "smapi_strip_prefix": smapi_strip_prefix,
            "cp77_prefix": cp77_prefix,
            "has_manifest_json": has_manifest_json,
            "has_rdr2_files": has_rdr2_files,
            "has_pak_files": has_pak_files,
            "has_ue4ss_files": has_ue4ss_files,
            "has_esp_bsa": has_esp_bsa,
            "has_i18n": has_i18n,
            "is_single_root": is_single_root,
            "single_root_name": single_root_name
        }

    def determine_install_strategy(self, game_domain: str, game_root: Path, archive_path: Path) -> dict:
        profile = self.rules.get(game_domain, {})
        archive_info = self.inspect_archive_structure(archive_path)

        mods_subdir = profile.get("mods_subdir", "Mods")
        strip_root = profile.get("root_strip_if_single", True)
        strip_prefix = ""

        # 1. 赛博朋克2077 专属多目录合并与分类部署
        if game_domain == "cyberpunk2077":
            flist = archive_info["file_list"]
            if archive_info["cp77_prefix"] is not None:
                target_path = game_root
                mode = "merge_game_root"
                strip_prefix = archive_info["cp77_prefix"]
            elif any(f.lower().endswith((".archive", ".xl")) for f in flist):
                target_path = game_root / "archive" / "pc" / "mod"
                mode = "flat_files"
            elif any(f.lower().endswith(".reds") for f in flist):
                target_path = game_root / "r6" / "scripts"
                mode = "direct_extract_with_root" if archive_info["is_single_root"] else "subfolder_per_mod"
            elif any(Path(f).name.lower() == "init.lua" for f in flist):
                target_path = game_root / "bin" / "x64" / "plugins" / "cyber_engine_tweaks" / "mods"
                mode = "direct_extract_with_root" if archive_info["is_single_root"] else "subfolder_per_mod"
            else:
                target_path = game_root / "archive" / "pc" / "mod"
                mode = "subfolder_per_mod"

        # 2. 影之刃零 (区分 PAK 模组、UE4SS 模组及 UE4SS+PAK 混合模组)
        elif game_domain in ("phantomblandzero", "phantombladezero"):
            flist = archive_info["file_list"]
            # 若包内本身按游戏根目录组织了 ProjectPBZ/ 或 platform/
            pbz_roots = {"projectpbz", "platform"}
            if any(f.split("/")[0].lower() in pbz_roots for f in flist):
                target_path = game_root
                mode = "merge_game_root"
            elif archive_info["is_single_root"] and any(len(f.split("/")) > 1 and f.split("/")[1].lower() in pbz_roots for f in flist):
                target_path = game_root
                strip_prefix = archive_info["single_root_name"] or ""
                mode = "merge_game_root"
            elif archive_info["has_pak_files"] and archive_info["has_ue4ss_files"]:
                target_path = game_root / "platform" / "steam" / "ue4ss" / "Mods"
                mode = "ue4ss_and_pak"
            elif archive_info["has_pak_files"] and not archive_info["has_ue4ss_files"]:
                target_path = game_root / "ProjectPBZ" / "Content" / "Paks" / "~mods"
                mode = "pak_folder"
            else:
                target_path = game_root / "platform" / "steam" / "ue4ss" / "Mods"
                # 检查是否双层包裹了 Scripts/main.lua
                lua_paths = [f.split("/") for f in flist if f.lower().endswith(("scripts/main.lua", "dlls/main.dll"))]
                if lua_paths:
                    min_len = min(len(lp) for lp in lua_paths)
                    if min_len == 2:
                        mode = "subfolder_per_mod"
                    elif min_len == 3:
                        mode = "direct_extract_with_root"
                    else:
                        strip_prefix = "/".join(lua_paths[0][:-3])
                        mode = "strip_prefix_extract"
                else:
                    mode = "direct_extract_with_root" if archive_info["is_single_root"] else "subfolder_per_mod"

        # 3. 其他虚幻引擎 PAK 游戏
        elif archive_info["has_pak_files"]:
            target_path = game_root / (mods_subdir if mods_subdir else "~mods")
            mode = "pak_folder"

        # 4. 荒野大镖客2 (LML 与根路径 .asi 分流)
        elif archive_info["has_rdr2_files"] or game_domain == "reddeadredemption2":
            flist = archive_info["file_list"]
            if any(f.lower().startswith("lml/") for f in flist):
                target_path = game_root
                mode = "merge_game_root"
            elif any(Path(f).name.lower() == "install.xml" for f in flist):
                target_path = game_root / "lml"
                xml_parts = [f.split("/") for f in flist if Path(f).name.lower() == "install.xml"]
                min_len = min(len(p) for p in xml_parts)
                if min_len == 1:
                    mode = "subfolder_per_mod"
                elif min_len == 2:
                    mode = "direct_extract_with_root"
                else:
                    strip_prefix = "/".join(xml_parts[0][:-2])
                    mode = "strip_prefix_extract"
            elif any(f.lower().endswith(".asi") for f in flist):
                target_path = game_root
                if archive_info["is_single_root"]:
                    strip_prefix = archive_info["single_root_name"] or ""
                mode = "merge_game_root"
            else:
                target_path = game_root / (mods_subdir if mods_subdir else "lml")
                mode = "direct_extract_with_root" if archive_info["is_single_root"] else "subfolder_per_mod"

        # 5. 经典 Bethesda 游戏 (.esp/.esm)
        elif archive_info["has_esp_bsa"]:
            target_path = game_root / "Data"
            mode = "merge_game_root"

        # 6. 星露谷 SMAPI 或通用子目录模组（含自动去双层嵌套）
        else:
            base_dir = game_root / mods_subdir if mods_subdir else game_root
            target_path = base_dir
            if archive_info["smapi_strip_prefix"]:
                strip_prefix = archive_info["smapi_strip_prefix"]
                mode = "strip_prefix_extract"
            elif archive_info["manifest_paths"]:
                min_m_depth = min(len(mp.split("/")) for mp in archive_info["manifest_paths"])
                if min_m_depth == 1:
                    mode = "subfolder_per_mod"
                else:
                    mode = "direct_extract_with_root"
            elif archive_info["is_single_root"] and strip_root:
                mode = "direct_extract_with_root"
            else:
                mode = "subfolder_per_mod"

        return {
            "game_domain": game_domain,
            "target_path": target_path,
            "strip_prefix": strip_prefix,
            "mode": mode,
            "archive_info": archive_info,
            "profile": profile
        }

    def install_mod_archive(self, archive_path: Path, game_domain: str, game_root: Path, mod_name: str) -> Dict[str, Any]:
        """
        自适应执行模组安装解压：
          - 支持 .zip / .7z / .rar 魔数自动识别解压
          - 自动剥离双层/多层包裹目录 (strip_prefix_extract)
          - 支持 Cyberpunk 2077 跨目录合并 (merge_game_root) 与已部署文件级清单追踪
          - 支持 PBZ UE4SS 自动补全 enabled.txt 与 PAK 分流
        """
        strategy = self.determine_install_strategy(game_domain, game_root, archive_path)
        target_path = strategy["target_path"]
        target_path.mkdir(parents=True, exist_ok=True)
        arch_info = strategy["archive_info"]
        mode = strategy["mode"]
        strip_prefix = strategy.get("strip_prefix", "")

        safe_name = "".join("_" if c in r'\/:*?"<>|' else c for c in mod_name).strip() or "InstalledMod"
        final_installed_dir = None
        installed_files: List[str] = []

        if mode == "ue4ss_and_pak":
            import tempfile
            paks_dest = game_root / "ProjectPBZ" / "Content" / "Paks" / "~mods"
            paks_dest.mkdir(parents=True, exist_ok=True)
            target_path.mkdir(parents=True, exist_ok=True)
            pak_exts = (".pak", ".ucas", ".utoc", ".scripts.txt")
            with tempfile.TemporaryDirectory(prefix="mod_pbz_hybrid_") as st_str:
                st = Path(st_str)
                self._extract_raw_to_dir(archive_path, st)
                for item in st.rglob("*"):
                    if item.is_file() and item.name.lower().endswith(pak_exts):
                        out_p = paks_dest / item.name
                        shutil.copy2(item, out_p)
                        installed_files.append(f"ProjectPBZ/Content/Paks/~mods/{item.name}")
                        item.unlink(missing_ok=True)
                # 将剩余的 UE4SS 模组目录部署至 ue4ss/Mods
                lua_Anchors = [p for p in st.rglob("*") if p.is_file() and str(p).replace("\\", "/").lower().endswith(("scripts/main.lua", "dlls/main.dll"))]
                if lua_Anchors:
                    for anchor in lua_Anchors:
                        mod_root_dir = anchor.parents[1]
                        dest_mod_dir = target_path / (safe_name if mod_root_dir == st else mod_root_dir.name)
                        if dest_mod_dir.exists():
                            shutil.rmtree(dest_mod_dir)
                        shutil.copytree(mod_root_dir, dest_mod_dir)
                        if not (dest_mod_dir / "enabled.txt").exists():
                            (dest_mod_dir / "enabled.txt").write_text("", encoding="utf-8")
                        if final_installed_dir is None:
                            final_installed_dir = dest_mod_dir
                        else:
                            installed_files.extend(
                                str(f.relative_to(game_root)).replace("\\", "/")
                                for f in dest_mod_dir.rglob("*") if f.is_file()
                            )

        elif mode == "pak_folder":
            final_installed_dir = target_path
            extracted_names = self._extract_specific_extensions(archive_path, target_path, (".pak", ".ucas", ".utoc"))
            for fn in extracted_names:
                try:
                    installed_files.append(str((target_path / fn).relative_to(game_root)).replace("\\", "/"))
                except Exception:
                    installed_files.append(str((target_path / fn)).replace("\\", "/"))

        elif mode == "flat_files":
            final_installed_dir = target_path
            rel_extracted = self._extract_with_options(archive_path, target_path, flatten=True)
            for fn in rel_extracted:
                try:
                    installed_files.append(str((target_path / fn).relative_to(game_root)).replace("\\", "/"))
                except Exception:
                    installed_files.append(str((target_path / fn)).replace("\\", "/"))

        elif mode == "merge_game_root":
            final_installed_dir = target_path
            rel_extracted = self._extract_with_options(archive_path, target_path, strip_prefix=strip_prefix)
            installed_files = [f.replace("\\", "/") for f in rel_extracted]

        elif mode == "strip_prefix_extract":
            rel_extracted = self._extract_with_options(archive_path, target_path, strip_prefix=strip_prefix)
            top_folders = {f.split("/")[0] for f in rel_extracted if "/" in f}
            if len(top_folders) == 1:
                final_installed_dir = target_path / list(top_folders)[0]
            else:
                final_installed_dir = target_path
                for f in rel_extracted:
                    try:
                        installed_files.append(str((target_path / f).relative_to(game_root)).replace("\\", "/"))
                    except Exception:
                        pass

        elif mode == "direct_extract_with_root":
            rel_extracted = self._extract_with_options(archive_path, target_path)
            top_folders = {f.split("/")[0] for f in rel_extracted if "/" in f}
            if arch_info["single_root_name"]:
                final_installed_dir = target_path / arch_info["single_root_name"]
            elif len(top_folders) == 1:
                final_installed_dir = target_path / list(top_folders)[0]
            else:
                final_installed_dir = target_path
                for f in rel_extracted:
                    try:
                        installed_files.append(str((target_path / f).relative_to(game_root)).replace("\\", "/"))
                    except Exception:
                        pass

        else:
            dest = target_path / safe_name
            dest.mkdir(parents=True, exist_ok=True)
            self._extract_with_options(archive_path, dest)
            final_installed_dir = dest

        # 针对影之刃零 UE4SS 模组：若解压目录内含 Scripts/main.lua 或 dlls/main.dll，确保 enabled.txt 存在以自动启用
        if game_domain in ("phantomblandzero", "phantombladezero") and final_installed_dir and final_installed_dir.is_dir():
            has_lua_or_dll = (
                (final_installed_dir / "Scripts" / "main.lua").exists() or
                (final_installed_dir / "scripts" / "main.lua").exists() or
                (final_installed_dir / "dlls" / "main.dll").exists()
            )
            if has_lua_or_dll and not (final_installed_dir / "enabled.txt").exists():
                try:
                    (final_installed_dir / "enabled.txt").write_text("", encoding="utf-8")
                except Exception:
                    pass

        return {
            "strategy": strategy,
            "installed_dir": final_installed_dir,
            "target_path": target_path,
            "installed_files": installed_files
        }

    def _extract_raw_to_dir(self, archive: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        fmt = self.detect_archive_format(archive)
        if fmt == "zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
            return
        if fmt == "7z" and HAS_PY7ZR:
            try:
                with py7zr.SevenZipFile(archive, mode="r") as sz:
                    sz.extractall(path=dest)
                return
            except Exception:
                pass

        ext_tool = self._find_external_extractor()
        if not ext_tool:
            raise RuntimeError(f"无法解压格式 [{fmt}]: 未找到 py7zr、7-Zip 或系统 tar.exe")
        kind, exe = ext_tool
        import subprocess
        if kind == "7z":
            subprocess.run([exe, "x", str(archive), f"-o{dest}", "-y"], check=True, stdout=subprocess.DEVNULL)
        else:
            subprocess.run([exe, "-xf", str(archive), "-C", str(dest)], check=True)

    def _extract_all(self, archive: Path, dest: Path) -> None:
        self._extract_raw_to_dir(archive, dest)

    def _extract_with_options(self, archive: Path, dest: Path, strip_prefix: str = "", flatten: bool = False) -> List[str]:
        """支持前缀目录剥离 (去双层嵌套) 与扁平提取，并返回相对于 dest 的落地文件路径列表"""
        import tempfile
        dest.mkdir(parents=True, exist_ok=True)
        clean_prefix = strip_prefix.replace("\\", "/").strip("/")
        extracted_rels: List[str] = []

        with tempfile.TemporaryDirectory(prefix="mod_extract_") as staging_str:
            staging = Path(staging_str)
            self._extract_raw_to_dir(archive, staging)

            base_src = staging / clean_prefix if clean_prefix and (staging / clean_prefix).exists() else staging
            for item in base_src.rglob("*"):
                if item.is_dir():
                    continue
                rel = Path(item.name) if flatten else item.relative_to(base_src)
                out_file = dest / rel
                out_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, out_file)
                extracted_rels.append(str(rel).replace("\\", "/"))
        return extracted_rels

    def _extract_specific_extensions(self, archive: Path, dest: Path, extensions: tuple) -> List[str]:
        import tempfile
        dest.mkdir(parents=True, exist_ok=True)
        extracted = []
        with tempfile.TemporaryDirectory(prefix="mod_ext_filter_") as staging_str:
            staging = Path(staging_str)
            self._extract_raw_to_dir(archive, staging)
            for item in staging.rglob("*"):
                if item.is_file() and item.name.lower().endswith(extensions):
                    out_path = dest / item.name
                    shutil.copy2(item, out_path)
                    extracted.append(item.name)
        return extracted

    def check_dependencies(self, mod_path: Path, all_installed_mods: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        检查指定模组的前置依赖项是否已经满足
        返回: {"satisfied": bool, "missing": [...], "installed_deps": [...]}
        """
        manifest_file = mod_path / "manifest.json" if mod_path.is_dir() else None
        if not manifest_file or not manifest_file.exists():
            return {"satisfied": True, "missing": [], "installed_deps": []}

        try:
            data = _load_json_flexible(manifest_file.read_text(encoding="utf-8-sig"))
            raw_deps = list(data.get("Dependencies") or [])
            cp_for = data.get("ContentPackFor")
            if isinstance(cp_for, dict) and (cp_for.get("UniqueID") or cp_for.get("uniqueID")):
                raw_deps.insert(0, {
                    "UniqueID": cp_for.get("UniqueID") or cp_for.get("uniqueID"),
                    "MinimumVersion": cp_for.get("MinimumVersion"),
                    "IsRequired": True
                })
        except Exception:
            return {"satisfied": True, "missing": [], "installed_deps": []}

        installed_uids = {m.get("unique_id", "").lower(): m for m in all_installed_mods if m.get("unique_id")}
        # 也加入文件夹名称作为兼容匹配
        installed_folders = {m.get("folder", "").lower(): m for m in all_installed_mods if m.get("folder")}

        missing = []
        satisfied = []

        for dep in raw_deps:
            uid = dep.get("UniqueID") or dep.get("uniqueID") or ""
            is_required = dep.get("IsRequired", True)
            min_ver = dep.get("MinimumVersion")

            matched = installed_uids.get(uid.lower()) or installed_folders.get(uid.lower())
            if not matched:
                if is_required:
                    missing.append({"unique_id": uid, "required": True, "min_version": min_ver})
            else:
                satisfied.append({"unique_id": uid, "installed_version": matched.get("version")})

        return {
            "satisfied": len(missing) == 0,
            "missing": missing,
            "installed_deps": satisfied
        }

    def apply_i18n_translation(self, target_mod_dir: Path, translation_archive: Path) -> Dict[str, Any]:
        """
        汉化包覆盖：智能解压汉化文件并合并至 target_mod_dir/i18n 目录（支持 .zip/.7z/.rar）
        严格防止覆盖用户的 config.json、manifest.json 等核心配置
        """
        import tempfile
        if not target_mod_dir.exists():
            raise FileNotFoundError(f"目标模组目录不存在: {target_mod_dir}")

        target_i18n = target_mod_dir / "i18n"
        target_i18n.mkdir(parents=True, exist_ok=True)

        copied_files = []
        with tempfile.TemporaryDirectory(prefix="mod_i18n_") as staging_str:
            staging = Path(staging_str)
            self._extract_raw_to_dir(translation_archive, staging)
            for item in staging.rglob("*"):
                if item.is_dir():
                    continue
                fname = item.name
                rel_str = str(item.relative_to(staging)).lower()
                if fname.lower() in ("manifest.json", "config.json"):
                    continue
                if "i18n" in rel_str or fname.lower().endswith((".json", ".txt")):
                    out_path = target_i18n / fname
                    shutil.copy2(item, out_path)
                    copied_files.append(fname)

        return {
            "target_dir": str(target_i18n),
            "files_installed": copied_files,
            "success": len(copied_files) > 0
        }

    def get_backup_dir(self, mods_path: Path, game_domain: Optional[str] = None, target_key: Optional[str] = None) -> Path:
        """
        获取删除/卸载模组的临时归档目录：
        按用户规范：统一放置在当前工作区 (PROJECT_ROOT) 下，并采用“先游戏分组，再模组分组”结构：
          PROJECT_ROOT / "deleted_mods" / <game_domain> / <target_key>
        """
        game_sub = game_domain or "stardewvalley"
        if not ("temp" in str(mods_path).lower() or "uninstall_test_" in str(mods_path).lower()):
            try:
                from config_manager import get_system_paths, load_config
                paths = get_system_paths(load_config())
                base = paths["backup_dir"] / game_sub
                if target_key:
                    return base / target_key
                return base
            except Exception:
                pass

        root = PROJECT_ROOT
        if not root or not root.exists() or ("temp" in str(mods_path).lower() or "uninstall_test_" in str(mods_path).lower()):
            root = mods_path.parent

        base = root / "deleted_mods" / game_sub
        if target_key:
            return base / target_key
        return base

    def uninstall_mod(self, mods_path: Path, mod_identifier: str, hard_delete: bool = False, game_domain: Optional[str] = None) -> Dict[str, Any]:
        """
        卸载删除模组：
          1. 支持按目录名、模组名（模糊匹配/忽略空格符号）或 UniqueID 查找已安装模组
          2. 从游戏 Mods 目录中物理安全移出至当前项目根目录 (PROJECT_ROOT/deleted_mods/<game_domain>/<mod_folder>)
          3. 在当前项目目录下的 installed_mods.json 中标记为 status: "uninstalled"，保留其 Nexus ID、版本与名称
        """
        import time
        registry = self.load_installed_registry(mods_path, game_domain)
        target_key = None
        target_mod = None

        query = mod_identifier.lower().strip()
        # 1. 优先精确匹配 key 或 unique_id
        for key, item in registry.items():
            if item.get("status") == "uninstalled":
                continue
            if key.lower() == query or item.get("unique_id", "").lower() == query:
                target_key = key
                target_mod = item
                break

        # 2. 模糊匹配名称与归一化匹配（忽略空格、下划线、大小写和版本数字差异）
        def _norm(s: str) -> str:
            return re.sub(r"[\s_\-\.0-9]+", "", str(s)).lower()

        q_norm = _norm(query)
        if not target_key:
            for key, item in registry.items():
                if item.get("status") == "uninstalled":
                    continue
                name = item.get("name", "").lower()
                zh_name = (item.get("chinese_name") or item.get("name_zh") or "").lower()
                k_norm = _norm(key)
                n_norm = _norm(name)
                u_norm = _norm(item.get("unique_id", ""))
                if (
                    query in name or name in query or
                    (zh_name and (query in zh_name or zh_name in query)) or
                    (q_norm and (q_norm in n_norm or n_norm in q_norm or q_norm in k_norm or k_norm in q_norm or q_norm in u_norm))
                ):
                    target_key = key
                    target_mod = item
                    break

        # 3. 检查文件系统目录名
        if not target_key and mods_path.exists():
            for d in mods_path.iterdir():
                if d.name.startswith("."):
                    continue
                d_norm = _norm(d.name)
                if query in d.name.lower() or (q_norm and (q_norm in d_norm or d_norm in q_norm)):
                    target_key = d.name
                    target_mod = registry.get(target_key, {
                        "name": d.name,
                        "unique_id": d.name.lower(),
                        "version": "未知",
                        "nexus_id": None,
                        "format": "folder"
                    })
                    break

        if not target_key or not target_mod:
            return {
                "success": False,
                "error": f"在已安装模组中未找到匹配 [{mod_identifier}] 的模组"
            }

        from config_manager import resolve_game_root, load_config
        game_root = resolve_game_root(game_domain or "stardewvalley", load_config(), mods_path)
        backup_target = self.get_backup_dir(mods_path, game_domain, target_key)
        deleted_path = None
        installed_files = target_mod.get("installed_files") or []

        # 情况 A：复合/扁平模组按记录的 installed_files 精确文件级移出，绝不误删共享根目录
        if installed_files:
            moved_any = False
            if not hard_delete:
                if backup_target.exists():
                    shutil.rmtree(backup_target) if backup_target.is_dir() else backup_target.unlink()
                backup_target.mkdir(parents=True, exist_ok=True)
            for rel_f in installed_files:
                src_f = Path(rel_f) if Path(rel_f).is_absolute() else (game_root / rel_f)
                if src_f.exists() and src_f.is_file():
                    if hard_delete:
                        src_f.unlink(missing_ok=True)
                    else:
                        rel_dest = Path(rel_f).name if Path(rel_f).is_absolute() else Path(rel_f)
                        dst_f = backup_target / rel_dest
                        dst_f.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src_f), str(dst_f))
                    moved_any = True
            deleted_path = f"{len(installed_files)} files under {game_root}" if moved_any else None
        else:
            # 情况 B：独立子目录或单文件模组（严格拦截误把 mods_path 或 game_root 整体移走）
            recorded_path = target_mod.get("path")
            if recorded_path and Path(recorded_path).exists():
                mod_fs_path = Path(recorded_path)
            else:
                mod_fs_path = mods_path / target_key

            protected_roots = {mods_path.resolve(), game_root.resolve()}
            if mod_fs_path.exists() and mod_fs_path.resolve() in protected_roots:
                return {
                    "success": False,
                    "error": f"安全保护拦截：模组 [{target_key}] 指向共享根目录 ({mod_fs_path}) 且缺少文件明细，拒绝整目录移除"
                }

            if mod_fs_path.exists():
                deleted_path = str(mod_fs_path)
                if hard_delete:
                    if mod_fs_path.is_dir():
                        shutil.rmtree(mod_fs_path)
                    else:
                        mod_fs_path.unlink()
                else:
                    backup_target.parent.mkdir(parents=True, exist_ok=True)
                    if backup_target.exists():
                        if backup_target.is_dir():
                            shutil.rmtree(backup_target)
                        else:
                            backup_target.unlink()
                    shutil.move(str(mod_fs_path), str(backup_target))

        # 在清单中更新状态为 uninstalled 并持久化到项目目录
        target_mod["status"] = "uninstalled"
        target_mod["uninstalled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        target_mod["backup_path"] = str(backup_target) if not hard_delete else None
        registry[target_key] = target_mod
        self.save_installed_registry(mods_path, registry, game_domain)

        return {
            "success": True,
            "mod_name": target_mod.get("name"),
            "key": target_key,
            "nexus_id": target_mod.get("nexus_id"),
            "version": target_mod.get("version"),
            "deleted_path": deleted_path,
            "backup_path": str(backup_target) if not hard_delete else None,
            "archived_to_backup": not hard_delete
        }

    def reinstall_mod(self, mods_path: Path, mod_identifier: str, game_domain: Optional[str] = None) -> Dict[str, Any]:
        """
        恢复/重新安装已卸载标记的模组：
          支持独立子目录模组与复合多文件模组 (installed_files) 的精准还原
        """
        registry = self.load_installed_registry(mods_path, game_domain)
        target_key = None
        target_mod = None

        def _norm(s: str) -> str:
            return re.sub(r"[\s_\-\.0-9]+", "", str(s)).lower()

        query = mod_identifier.lower().strip()
        q_norm = _norm(query)
        for key, item in registry.items():
            if item.get("status") == "uninstalled":
                name = item.get("name", "").lower()
                zh_name = (item.get("chinese_name") or item.get("name_zh") or "").lower()
                uid = item.get("unique_id", "").lower()
                k_norm = _norm(key)
                n_norm = _norm(name)
                u_norm = _norm(uid)
                if (
                    key.lower() == query or uid == query or query in name or name in query or
                    (zh_name and (query in zh_name or zh_name in query)) or
                    (q_norm and (q_norm in n_norm or n_norm in q_norm or q_norm in k_norm or k_norm in q_norm or q_norm in u_norm))
                ):
                    target_key = key
                    target_mod = item
                    break

        if not target_key or not target_mod:
            return {"success": False, "error": f"在已卸载/已删除记录中未找到 [{mod_identifier}]"}

        backup_source = None
        recorded_backup = target_mod.get("backup_path")
        if recorded_backup and Path(recorded_backup).exists():
            backup_source = Path(recorded_backup)
        else:
            default_backup = self.get_backup_dir(mods_path, game_domain, target_key)
            if default_backup.exists():
                backup_source = default_backup
            elif (mods_path / ".uninstalled_backup" / target_key).exists():
                backup_source = mods_path / ".uninstalled_backup" / target_key

        from config_manager import resolve_game_root, load_config
        game_root = resolve_game_root(game_domain or "stardewvalley", load_config(), mods_path)
        installed_files = target_mod.get("installed_files") or []
        restored = False

        if backup_source and backup_source.exists():
            if installed_files and backup_source.is_dir():
                for item in backup_source.rglob("*"):
                    if item.is_file():
                        rel = item.relative_to(backup_source)
                        dst = game_root / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(item), str(dst))
                shutil.rmtree(backup_source, ignore_errors=True)
                restored = True
            else:
                recorded_path = target_mod.get("path")
                target_dest = Path(recorded_path) if recorded_path else (mods_path / target_key)
                if target_dest.exists():
                    if target_dest.is_dir():
                        shutil.rmtree(target_dest)
                    else:
                        target_dest.unlink()
                target_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup_source), str(target_dest))
                restored = True

        target_mod["status"] = "installed"
        target_mod.pop("uninstalled_at", None)
        target_mod.pop("backup_path", None)
        registry[target_key] = target_mod
        self.save_installed_registry(mods_path, registry, game_domain)

        return {
            "success": True,
            "mod_name": target_mod.get("name"),
            "key": target_key,
            "nexus_id": target_mod.get("nexus_id"),
            "version": target_mod.get("version"),
            "restored_from_backup": restored
        }


    def list_uninstalled_mods(self, mods_path: Optional[Path] = None, game_domain: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取所有已标记卸载的模组列表（按游戏分组支持）"""
        reg_file = self.get_installed_registry_path(mods_path, game_domain)
        uninstalled = []
        if reg_file.exists():
            try:
                raw = json.loads(reg_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    has_grouping = any(isinstance(v, dict) and not _is_mod_entry(v) for v in raw.values())
                    if has_grouping:
                        for gname, gmods in raw.items():
                            if game_domain and gname != game_domain:
                                continue
                            if isinstance(gmods, dict):
                                for k, m in gmods.items():
                                    if m.get("status") == "uninstalled":
                                        uninstalled.append({"key": k, "game_domain": gname, **m})
                    else:
                        for k, m in raw.items():
                            if m.get("status") == "uninstalled":
                                uninstalled.append({"key": k, "game_domain": game_domain or "stardewvalley", **m})
            except Exception:
                pass
        return uninstalled