"""
scan_single_game.py — 单游戏专属模组定向扫描工具 (职责单一，严禁全盘全跑)

用法：
  python scan_single_game.py --game stardewvalley
  python scan_single_game.py --game cyberpunk2077
  python scan_single_game.py --game reddeadredemption2
  python scan_single_game.py --game phantomblandzero
"""

import sys
import json
import argparse
from pathlib import Path

# 确保脚本目录在 sys.path 中
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from adaptive_installer import AdaptiveInstallEngine
from config_manager import (
    load_config, save_config, normalize_game_domain,
    get_game_config, resolve_mods_path, find_project_root
)


def scan_single(game_input: str, custom_mods_path: str = None, verbose: bool = True):
    cfg = load_config()
    game_domain = normalize_game_domain(game_input)

    # 严格校验：必须是 mod_config.json 中已配置的游戏
    games = cfg.get("games", {})
    if game_domain not in games:
        print(f"[拒绝] 游戏 [{game_domain}] 未在 mod_config.json 的已授权列表中！")
        print(f"当前允许的游戏仅限: {list(games.keys())}")
        print("如需添加新游戏，请先明确在 mod_config.json 中配置，系统绝不擅自自动追加！")
        sys.exit(1)

    ginfo = games[game_domain]
    mods_path = resolve_mods_path(custom_mods_path, game_domain, cfg)
    if not mods_path or not mods_path.exists():
        print(f"[提示] 模组目录尚不存在: {mods_path}")
        return []

    print(f"\n========================================================")
    print(f" 🎯 单游戏定向扫描: {ginfo.get('name', game_domain)} [{game_domain}]")
    print(f" 📁 扫描路径: {mods_path}")
    print(f"========================================================")

    engine = AdaptiveInstallEngine()
    # 严格只扫描目标游戏，只维护其专属清单 installed_mods/<game_domain>.json
    scanned_mods = engine.scan_installed_mods(mods_path, game_domain)

    # 同步该游戏的统计数量与相对清单路径到 mod_config.json
    ginfo["installed_mods_count"] = len(scanned_mods)
    reg_file = engine.get_installed_registry_path(mods_path, game_domain=game_domain)
    # 使用跨电脑便携相对路径格式
    ginfo["installed_mods_file"] = f"./installed_mods/{game_domain}.json"
    if game_domain == cfg.get("defaultGame"):
        cfg.setdefault("detected_summary", {})["primary_game_mods_count"] = len(scanned_mods)

    save_config(cfg)

    has_nexus = sum(1 for m in scanned_mods if m.get("nexus_id"))
    print(f"\n✓ 扫描完成！共登记活跃模组: {len(scanned_mods)} 个 (含 Nexus ID: {has_nexus} 个)")
    print(f"  专属清单路径: {reg_file}")

    if verbose and scanned_mods:
        print(f"\n  【模组明细预览 (前 30 项)】:")
        for idx, m in enumerate(scanned_mods[:30], 1):
            zh_name = m.get("chinese_name") or m.get("name_zh")
            disp_name = f"{m.get('name')} [{zh_name}]" if zh_name and zh_name != m.get("name") else m.get("name")
            nid_str = f"[Nexus ID: {m.get('nexus_id')}]" if m.get("nexus_id") else "[无Nexus关联]"
            ver_str = f"(v{m.get('version', '未知')})"
            fmt_str = f"格式: {m.get('format', '未知')}"
            print(f"    {idx:2d}. {disp_name} {ver_str} {nid_str} {fmt_str}")
        if len(scanned_mods) > 30:
            print(f"    ... 以及其他 {len(scanned_mods)-30} 个模组 (已完整保存至专属清单)")

    print(f"========================================================\n")
    return scanned_mods


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="单游戏专属模组定向扫描工具 (严禁全盘全跑)")
    parser.add_argument("-g", "--game", required=True, help="游戏名称或别名 (如 stardewvalley / cyberpunk2077 / rdr2 / pbz)")
    parser.add_argument("-m", "--mods-path", help="自定义 Mods 路径 (可选)")
    parser.add_argument("-q", "--quiet", action="store_true", help="不打印明细，仅更新清单")
    args = parser.parse_args()

    scan_single(args.game, custom_mods_path=args.mods_path, verbose=not args.quiet)
