"""
manage_mod.py — 单模组专属管理工具 (卸载归档、一键恢复与单游戏清单查看，绝不全盘全跑)

用法：
  # 查看单游戏模组清单
  python manage_mod.py --game stardewvalley --list
  # 卸载指定模组（软删除并安全归档）
  python manage_mod.py --game stardewvalley --uninstall "Automate"
  # 恢复已卸载模组
  python manage_mod.py --game stardewvalley --reinstall "Automate"
  # 查看单游戏的已卸载历史
  python manage_mod.py --game stardewvalley --list-uninstalled
"""

import sys
import json
import argparse
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from adaptive_installer import AdaptiveInstallEngine
from config_manager import (
    load_config, save_config, normalize_game_domain,
    get_game_config, resolve_mods_path, resolve_game_root, find_project_root
)


def main():
    parser = argparse.ArgumentParser(description="单模组专属管理工具")
    parser.add_argument("-g", "--game", required=True, help="目标游戏 (如 stardewvalley / cyberpunk2077 / rdr2 / pbz)")
    parser.add_argument("--list", action="store_true", help="仅查看该游戏的已安装模组清单")
    parser.add_argument("--uninstall", help="卸载指定模组（安全归档并标记）")
    parser.add_argument("--reinstall", help="恢复重新安装已卸载的模组")
    parser.add_argument("--list-uninstalled", action="store_true", help="查看该游戏已归档/卸载的模组")
    parser.add_argument("--hard-delete", action="store_true", help="卸载时物理硬删除（不建议）")
    args = parser.parse_args()

    cfg = load_config()
    game_domain = normalize_game_domain(args.game)

    games = cfg.get("games", {})
    if game_domain not in games:
        print(f"[拒绝] 游戏 [{game_domain}] 未在 mod_config.json 中配置！")
        sys.exit(1)

    ginfo = games[game_domain]
    mods_path = resolve_mods_path(None, game_domain, cfg)
    game_root = resolve_game_root(game_domain, cfg, mods_path)
    engine = AdaptiveInstallEngine()

    # 1. 卸载（先校验游戏磁盘是否已挂载，防止盘符离线时仅改了清单却未移动真实文件）
    if args.uninstall:
        if not game_root.exists():
            print(f"✗ [磁盘未挂载] 游戏 [{game_domain}] 根目录当前不可访问: {game_root}")
            print("  请先连接游戏所在移动硬盘/磁盘后再执行卸载操作。")
            sys.exit(1)
        print(f"\n[卸载执行] 正在从 [{game_domain}] 卸载模组: {args.uninstall} ...")
        un_res = engine.uninstall_mod(mods_path, args.uninstall, hard_delete=args.hard_delete, game_domain=game_domain)
        if un_res["success"]:
            print(f"✓ 模组 [{un_res['mod_name']}] (Key: {un_res['key']}) 文件已清理。")
            if un_res.get("archived_to_backup"):
                print(f"  已安全归档至: {un_res.get('backup_path')}")
            print(f"✓ 已在该游戏专属清单中标记为 [已卸载/已删除]")
            if un_res.get("nexus_id"):
                print(f"  永久保留 Nexus Mod ID: {un_res['nexus_id']}")
        else:
            print(f"✗ 卸载失败: {un_res['error']}")
            sys.exit(1)
        return

    # 2. 恢复
    if args.reinstall:
        if not game_root.exists():
            print(f"✗ [磁盘未挂载] 游戏 [{game_domain}] 根目录当前不可访问: {game_root}")
            print("  请先连接游戏所在移动硬盘/磁盘后再执行恢复操作。")
            sys.exit(1)
        print(f"\n[恢复执行] 正在为 [{game_domain}] 重新安装已卸载模组: {args.reinstall} ...")
        re_res = engine.reinstall_mod(mods_path, args.reinstall, game_domain=game_domain)
        if re_res["success"]:
            if re_res.get("restored_from_backup"):
                print(f"✓ 模组 [{re_res['mod_name']}] 已成功从归档中还原至游戏目录。")
            print(f"✓ 状态已恢复为 [已安装]")
        else:
            print(f"✗ 恢复安装失败: {re_res['error']}")
            sys.exit(1)
        return

    # 3. 查看已卸载
    if args.list_uninstalled:
        un_list = engine.list_uninstalled_mods(mods_path, game_domain=game_domain)
        print(f"\n📦 [{ginfo.get('name', game_domain)}] 已卸载模组归档记录 (共 {len(un_list)} 项):")
        if not un_list:
            print("  (暂无已归档卸载的模组)")
        else:
            for idx, m in enumerate(un_list, 1):
                nid_tag = f"[Nexus ID: {m.get('nexus_id')}]" if m.get("nexus_id") else "[无ID]"
                print(f"  {idx:2d}. {m.get('name')} (v{m.get('version', '未知')}) {nid_tag} - 卸载于: {m.get('uninstalled_at', '未知')}")
        print()
        return

    # 4. 查看当前清单
    if args.list:
        registry = engine.load_installed_registry(mods_path, game_domain=game_domain)
        installed = [m for m in registry.values() if m.get("status") != "uninstalled"]
        uninstalled = [m for m in registry.values() if m.get("status") == "uninstalled"]
        print(f"\n🎮 游戏: {ginfo.get('name', game_domain)} [{game_domain}]")
        print(f"  ├─ 模组路径: {mods_path}")
        print(f"  ├─ 专属清单: {engine.get_installed_registry_path(mods_path, game_domain)}")
        print(f"  └─ 统计: 已安装 {len(installed)} 个 | 已归档卸载 {len(uninstalled)} 个")
        print(f"\n  【活跃模组明细】:")
        for idx, m in enumerate(installed, 1):
            zh_name = m.get("chinese_name") or m.get("name_zh")
            disp_name = f"{m.get('name')} [{zh_name}]" if zh_name and zh_name != m.get("name") else m.get("name")
            nid_str = f"[Nexus ID: {m.get('nexus_id')}]" if m.get("nexus_id") else "[无Nexus关联]"
            ver_str = f"(v{m.get('version', '未知')})"
            fmt_str = f"格式: {m.get('format', '未知')}"
            print(f"    {idx:2d}. {disp_name} {ver_str} {nid_str} {fmt_str}")
        print()


if __name__ == "__main__":
    main()
