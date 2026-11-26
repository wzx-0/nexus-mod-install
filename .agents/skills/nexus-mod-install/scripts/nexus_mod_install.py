#!/usr/bin/env python3
"""
nexus_mod_install.py — Nexus 模组搜索、四级自动下载通道、自适应安装、依赖联装与清单管理工具

核心能力：
  1. 严格遵循 mod_config.json 游戏白名单与单游戏隔离规范（未经 --add-game 绝不擅自添加未授权游戏）。
  2. 支持多入口指定目标模组：
     - 自然语言中英文搜索 (-q "钓鱼简化" / -q "CJB Cheats Menu")
     - Nexus Mod ID 直装 (--mod-id 2697 [-f 12345])
     - Nexus 网页链接直装 (--url "https://www.nexusmods.com/stardewvalley/mods/2697")
     - NXM 授权协议链接 (--nxm "nxm://...")
     - 本地压缩包直装 (--install-zip "xxx.zip/.7z/.rar")
  3. 四级自动下载回退链路（免费账号遇到 HTTP 403 零阻塞）：
     Tier 1: Nexus 官方高速 CDN API (Premium 账号或含 NXM 临时授权 Key)
     Tier 2: 官方 GitHub Release / 验证开源镜像直连（支持动态拉取最新 Release 资产）
     Tier 3: 本地下载目录智能接管（Downloads / tmp_downloads / extraDownloadDirs）
     Tier 4: 桌面 Chrome 会话热复用 + 原生 CDP 自动点击 Slow Download 下载捕获（默认开启，传 --no-simulate-browser 可关闭）
  4. 自适应解压部署：去双层嵌套、魔数识别 (.zip/.7z/.rar)、2077 跨目录合并、影之刃零 UE4SS/PAK 分流、RDR2 LML/ASI 分流。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional, List, Dict, Any
from urllib.parse import unquote, urlparse

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from adaptive_installer import AdaptiveInstallEngine, find_project_root, get_stardew_chinese_name
from config_manager import (
    load_config, get_system_paths, normalize_game_domain, get_nexus_domain,
    get_game_config, resolve_api_key, resolve_mods_path, resolve_game_root,
    parse_nexus_reference, get_search_keywords, get_upstream_mirrors, get_mod_translations,
    learn_search_keyword, learn_game_alias, load_mappings
)


def _ensure_deps():
    import importlib
    missing = []
    for pkg, imp in [("requests", "requests"), ("py7zr", "py7zr"), ("tqdm", "tqdm")]:
        try:
            importlib.import_module(imp)
        except ImportError:
            missing.append(pkg)
    if missing:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install"] + missing,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass


_ensure_deps()

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    requests = None
    HAS_REQUESTS = False

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable=None, *args, **kwargs):
        class _DummyBar:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def update(self, *a, **kw): pass
        if iterable is not None and hasattr(iterable, "__iter__"):
            return iterable
        return _DummyBar()

PROJECT_ROOT = find_project_root()
PROJECT_CONFIG = PROJECT_ROOT / "mod_config.json"

NEXUS_API_BASE = "https://api.nexusmods.com/v1"
NEXUS_SEARCH = "https://search.nexusmods.com/mods"
NEXUS_GRAPHQL = "https://api.nexusmods.com/v2/graphql"


def resolve_tmp_dir(arg: Optional[str], cfg: dict) -> Path:
    if arg:
        return Path(arg)
    try:
        return get_system_paths(cfg)["tmp_downloads_dir"]
    except Exception:
        return PROJECT_ROOT / "tmp_downloads"


def sanitize_name(name: str) -> str:
    return "".join("_" if c in r'\/:*?"<>|' else c for c in str(name)).strip() or "ModPackage"


class NexusAPI:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({
            "apiKey": api_key,
            "User-Agent": "nexus-mod-install-skill/3.0",
            "Accept": "application/json",
        })

    def _get(self, url: str, params: dict = None) -> dict:
        r = self.session.get(url, params=params, timeout=25)
        if r.status_code == 429:
            raise RuntimeError("Nexus API 速率限制已达上限 (HTTP 429)")
        if r.status_code == 401:
            raise RuntimeError("Nexus API Key 无效 (HTTP 401)")
        r.raise_for_status()
        return r.json()

    def get_game_id(self, game_domain: str) -> Optional[int]:
        try:
            d = self._get(f"{NEXUS_API_BASE}/games/{get_nexus_domain(game_domain)}.json")
            return d.get("id")
        except Exception:
            return None

    def get_mod_info(self, game_domain: str, mod_id: int) -> Dict[str, Any]:
        try:
            return self._get(f"{NEXUS_API_BASE}/games/{get_nexus_domain(game_domain)}/mods/{mod_id}.json")
        except Exception:
            return {}

    def search_mods(self, keyword: str, game_id: int = 1303, game_domain: str = "stardewvalley", limit: int = 5) -> list:
        # 1. 优先调用官方 Nexus GraphQL v2 API（按下载量倒序，确保热门正统模组排第一）
        try:
            query = """
            query SearchMods($filter: ModsFilter, $sort: [ModsSort!]) {
              mods(filter: $filter, sort: $sort) {
                nodes {
                  modId
                  name
                  summary
                  version
                  downloads
                }
              }
            }
            """
            variables = {
                "filter": {
                    "gameDomainName": [{"value": get_nexus_domain(game_domain), "op": "EQUALS"}],
                    "name": [{"value": keyword, "op": "WILDCARD"}]
                },
                "sort": [{"downloads": {"direction": "DESC"}}]
            }
            r = self.session.post(
                NEXUS_GRAPHQL,
                json={"query": query, "variables": variables},
                timeout=15
            )
            if r.status_code == 200:
                nodes = r.json().get("data", {}).get("mods", {}).get("nodes", [])
                if nodes:
                    return [
                        {
                            "mod_id": int(n["modId"]),
                            "name": n["name"],
                            "summary": n.get("summary", ""),
                            "version": n.get("version", ""),
                            "mod_unique_downloads": n.get("downloads", 0)
                        }
                        for n in nodes[:limit]
                    ]
        except Exception:
            pass

        # 2. 回退传统搜索接口
        try:
            params = {"terms": keyword, "game_id": game_id, "include_adult": 0}
            r = requests.get(NEXUS_SEARCH, params=params, timeout=10)
            if r.status_code == 200:
                return r.json().get("items", [])[:limit]
        except Exception:
            pass
        return []

    def get_mod_requirements(self, mod_id: int, game_id: Optional[int] = 1303) -> dict:
        if not game_id:
            return {"mod_id": mod_id, "requirements": []}
        query = """
        query GetModReqs($modId: ID!, $gameId: ID!) {
          mod(modId: $modId, gameId: $gameId) {
            name
            summary
            version
            modRequirements {
              nexusRequirements {
                nodes {
                  modId
                  modName
                  notes
                  url
                  externalRequirement
                }
              }
            }
          }
        }
        """
        try:
            r = self.session.post(
                NEXUS_GRAPHQL,
                json={"query": query, "variables": {"modId": str(mod_id), "gameId": str(game_id)}},
                timeout=15
            )
            if r.status_code == 200:
                mod_data = r.json().get("data", {}).get("mod") or {}
                req_nodes = (mod_data.get("modRequirements") or {}).get("nexusRequirements", {}).get("nodes", [])
                return {
                    "mod_id": mod_id,
                    "name": mod_data.get("name"),
                    "version": mod_data.get("version"),
                    "requirements": req_nodes
                }
        except Exception:
            pass
        return {"mod_id": mod_id, "requirements": []}

    def get_mod_files(self, game_domain: str, mod_id: int, category: str = "main") -> list:
        url = f"{NEXUS_API_BASE}/games/{get_nexus_domain(game_domain)}/mods/{mod_id}/files.json"
        params = {"category": category} if category else {}
        try:
            return self._get(url, params).get("files", [])
        except Exception:
            return []

    def get_download_links(self, game_domain: str, mod_id: int, file_id, key: str = None, expires: str = None) -> list:
        url = f"{NEXUS_API_BASE}/games/{get_nexus_domain(game_domain)}/mods/{mod_id}/files/{file_id}/download_link.json"
        params = {}
        if key:
            params["key"] = key
        if expires:
            params["expires"] = str(expires)
        try:
            return self._get(url, params=params)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (402, 403):
                return []
            raise


def translate_mod_query(query: str, game_domain: str) -> str:
    """基于 mod_mappings.json 中的 search_keywords 将中文功能意图转换为最佳英文检索词"""
    if not query:
        return ""
    q_lower = query.lower().strip()
    kw_dicts = get_search_keywords(game_domain)

    # 按关键词长度降序匹配，优先命中更具体的词组（如 "作弊菜单" 优先于 "作弊"）
    for cn_kw, en_target in sorted(kw_dicts.get("game", {}).items(), key=lambda kv: len(kv[0]), reverse=True):
        if cn_kw.lower() in q_lower:
            return en_target

    for cn_kw, en_target in sorted(kw_dicts.get("general", {}).items(), key=lambda kv: len(kv[0]), reverse=True):
        if cn_kw.lower() in q_lower:
            return en_target
    return query


def resolve_mirror_download(mod_name: str, mod_id: Optional[int] = None, game_domain: str = "") -> Optional[Dict[str, Any]]:
    """
    匹配 mod_mappings.json 中的验证开源镜像，并优先尝试通过 GitHub Releases API 获取最新资产直链，
    若网络受限或触发速率限制则回退至预置稳定 URL。
    """
    mirrors = get_upstream_mirrors()
    name_low = (mod_name or "").lower().strip()
    norm_game = normalize_game_domain(game_domain) if game_domain else ""

    matched = None
    for mk, mdata in mirrors.items():
        m_game = mdata.get("game_domain")
        if norm_game and m_game and normalize_game_domain(m_game) != norm_game:
            continue
        if (mod_id and str(mdata.get("mod_id")) == str(mod_id)) or (mk.lower() in name_low):
            matched = dict(mdata)
            break

    if not matched:
        return None

    repo = matched.get("github_repo")
    if repo and HAS_REQUESTS:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "nexus-mod-install-skill/3.0"},
                timeout=6
            )
            if r.status_code == 200:
                rel = r.json()
                raw_tag = str(rel.get("tag_name") or "")
                ver_m = re.search(r"(\d+(?:\.\d+)+)", raw_tag)
                tag_ver = ver_m.group(1) if ver_m else (raw_tag.lstrip("vV") or matched.get("version", "latest"))
                assets = rel.get("assets") or []
                kw_req = (matched.get("asset_keyword") or "").lower()
                default_excludes = {"pdb", "symbols", "debug", "source", "sdk", "for-developers", "doc"}
                excludes = default_excludes.union({x.lower() for x in (matched.get("asset_exclude") or [])})

                for asset in assets:
                    aname = asset.get("name") or ""
                    alow = aname.lower()
                    if not alow.endswith((".zip", ".7z", ".rar")):
                        continue
                    if kw_req and kw_req not in alow:
                        continue
                    if any(ex in alow for ex in excludes):
                        continue
                    dl_url = asset.get("browser_download_url")
                    if dl_url:
                        matched["url"] = dl_url
                        matched["filename"] = aname
                        matched["version"] = tag_ver
                        break
        except Exception:
            pass

    if matched.get("url"):
        return matched
    return None


def find_recent_download(mod_name: str, mod_id: Optional[int] = None, minutes: int = 30, cfg: Optional[dict] = None) -> Optional[Path]:
    try:
        paths = get_system_paths(cfg)
        search_dirs = paths.get("all_download_dirs") or [paths["downloads_dir"], paths["tmp_downloads_dir"]]
    except Exception:
        search_dirs = [Path.home() / "Downloads"]

    cutoff = time.time() - (minutes * 60)
    candidates = []
    for downloads_dir in search_dirs:
        if not downloads_dir.exists():
            continue
        for ext in ("*.zip", "*.7z", "*.rar"):
            for f in downloads_dir.glob(ext):
                if Path(str(f) + ".crdownload").exists():
                    continue
                try:
                    mtime = f.stat().st_mtime
                    if mtime >= cutoff and f.stat().st_size > 100:
                        candidates.append((mtime, f))
                except Exception:
                    pass
    candidates.sort(key=lambda x: x[0], reverse=True)
    if not candidates:
        return None

    tokens = [t.lower() for t in re.split(r"[\s_\-]+", mod_name or "") if len(t) > 2]
    for _, f in candidates:
        fn = f.name.lower()
        if mod_id and re.search(rf"(^|[\-_ ]){mod_id}([\-_ .]|$)", fn):
            return f
        if tokens and all(tok in fn for tok in tokens[:2]):
            return f
    return None


def _infer_filename_from_response(url: str, resp, fallback_name: str) -> str:
    cd = resp.headers.get("Content-Disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
    if m:
        fname = unquote(m.group(1)).strip()
        if fname:
            return sanitize_name(fname)
    url_path_name = unquote(Path(urlparse(url).path).name)
    if url_path_name and any(url_path_name.lower().endswith(ext) for ext in (".zip", ".7z", ".rar")):
        return sanitize_name(url_path_name)
    return fallback_name


def download_file(url: str, dest: Path, label: str = "") -> Path:
    """支持断点续传、真实文件名推断、标准 TLS 校验及魔数合法性校验的稳健下载器"""
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) nexus-mod-install/3.0"}
    tmp = dest.with_suffix(dest.suffix + ".ftmp")
    start = tmp.stat().st_size if tmp.exists() else 0
    if start:
        hdrs["Range"] = f"bytes={start}-"

    try:
        r = requests.get(url, headers=hdrs, stream=True, timeout=60, verify=True)
    except requests.exceptions.SSLError:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.get(url, headers=hdrs, stream=True, timeout=60, verify=False)

    if start and r.status_code == 200:
        start = 0
        tmp.unlink(missing_ok=True)

    if r.status_code not in (200, 206):
        raise RuntimeError(f"下载失败：HTTP {r.status_code} ({url})")

    real_name = _infer_filename_from_response(url, r, dest.name)
    if real_name != dest.name and Path(real_name).suffix.lower() in (".zip", ".7z", ".rar"):
        dest = dest.parent / real_name
        new_tmp = dest.with_suffix(dest.suffix + ".ftmp")
        if tmp.exists() and tmp != new_tmp:
            tmp.replace(new_tmp)
        tmp = new_tmp

    total = int(r.headers.get("Content-Length", 0)) + start
    mode = "ab" if start else "wb"

    with open(tmp, mode) as f, \
         tqdm(total=total, initial=start, unit="B", unit_scale=True,
              unit_divisor=1024, desc=label or dest.name, ncols=80) as bar:
        for chunk in r.iter_content(65536):
            if chunk:
                f.write(chunk)
                bar.update(len(chunk))

    for i in range(3):
        try:
            tmp.replace(dest)
            break
        except OSError:
            if i < 2:
                time.sleep(0.2)
            else:
                raise

    # 校验并非错误 HTML 页面
    AdaptiveInstallEngine.detect_archive_format(dest)
    return dest


def run_browser_simulation_download(
    game_domain: str,
    mod_id: int,
    file_id: Optional[int],
    mod_name: str,
    cfg: dict,
    close_browser: bool = False,
    visible: bool = True,
    config_path: Optional[str] = None
) -> Optional[Path]:
    """调用会话热复用的桌面 Chrome 下载器（优先 Node 原生 CDP 快速通道，回退 Python CDP）"""
    mcp_script = CURRENT_DIR / "mcp_nexus_download.js"
    if mcp_script.exists() and shutil.which("node"):
        try:
            cmd = [
                "node", str(mcp_script),
                "-g", game_domain,
                "-m", str(mod_id),
                "--name", mod_name,
                "--download-only"
            ]
            if config_path:
                cmd.extend(["--config", str(config_path)])
            if file_id:
                cmd.extend(["-f", str(file_id)])
            if close_browser:
                cmd.append("--close-browser")

            print(f"\n[浏览器模拟直下] 正在通过桌面 Chrome 会话自动下载: {mod_name} (ID: {mod_id}) ...")
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
            captured_path: Optional[Path] = None
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="")
                if "[DOWNLOADED_ARCHIVE]" in line:
                    raw_p = line.split("[DOWNLOADED_ARCHIVE]", 1)[1].strip()
                    if raw_p and Path(raw_p).exists():
                        captured_path = Path(raw_p)
            proc.wait()
            if captured_path and captured_path.exists():
                return captured_path
        except Exception as me:
            print(f"[提示] Node CDP 通道异常，自动切换至 Python 原生 CDP 回退: {me}")

    try:
        from chrome_simulate_download import simulate_nexus_download
        return simulate_nexus_download(
            game_domain=game_domain,
            mod_id=mod_id,
            file_id=file_id,
            mod_name=mod_name,
            visible=visible,
            close_browser=close_browser
        )
    except Exception as e:
        print(f"[浏览器模拟失败] {e}")
        return None


def acquire_mod_archive(
    api: Optional[NexusAPI],
    game_domain: str,
    mod_id: int,
    mod_name: str,
    tmp_dir: Path,
    cfg: dict,
    file_id: Optional[int] = None,
    nxm_key: Optional[str] = None,
    nxm_expires: Optional[str] = None,
    allow_browser_sim: bool = True,
    close_browser: bool = False
) -> Dict[str, Any]:
    """
    统一四级模组压缩包获取管道：
      返回 {"archive": Optional[Path], "version": str, "mod_name": str, "source": str}
    """
    resolved_name = mod_name or f"Mod_{mod_id}"
    resolved_ver = "最新"
    main_fid = file_id
    target_filename = f"mod_{mod_id}.zip"

    # 1. 查询 Nexus 文件元数据与尝试官方 CDN API 下载
    if api:
        if resolved_name.startswith("Mod_"):
            info = api.get_mod_info(game_domain, mod_id)
            if info.get("name"):
                resolved_name = info["name"]
            if info.get("version"):
                resolved_ver = info["version"]

        files = api.get_mod_files(game_domain, mod_id, category="main") or api.get_mod_files(game_domain, mod_id, category="")
        chosen_file = None
        if files:
            if main_fid:
                chosen_file = next((f for f in files if str(f.get("file_id")) == str(main_fid)), None)
            if not chosen_file:
                chosen_file = max(files, key=lambda f: f.get("uploaded_timestamp", 0))
            if chosen_file:
                main_fid = chosen_file.get("file_id")
                resolved_ver = chosen_file.get("version") or resolved_ver
                if chosen_file.get("file_name"):
                    target_filename = sanitize_name(chosen_file["file_name"])

        if main_fid:
            links = api.get_download_links(game_domain, mod_id, main_fid, key=nxm_key, expires=nxm_expires)
            if links:
                dest = tmp_dir / target_filename
                print(f"\n[Tier 1 · 官方 CDN] 正在高速下载 [{resolved_name}] ...")
                arch = download_file(links[0]["URI"], dest, label=resolved_name)
                return {"archive": arch, "version": resolved_ver, "mod_name": resolved_name, "source": "nexus_cdn"}

    # 2. 官方验证开源镜像 / GitHub Release 最新资产直连
    mirror = resolve_mirror_download(resolved_name, mod_id=mod_id, game_domain=game_domain)
    if mirror and mirror.get("url"):
        print(f"\n[Tier 2 · 官方开源镜像] 自动切换高速直连镜像: {mirror['url']}")
        dest = tmp_dir / sanitize_name(mirror.get("filename") or target_filename)
        try:
            arch = download_file(mirror["url"], dest, label=mirror.get("name") or resolved_name)
            return {
                "archive": arch,
                "version": mirror.get("version") or resolved_ver,
                "mod_name": mirror.get("name") or resolved_name,
                "source": "upstream_mirror"
            }
        except Exception as me:
            print(f"[镜像提示] 镜像下载异常 ({me})，继续尝试下一通道...")

    # 3. 检查本地下载目录近期是否已有匹配安装包
    recent = find_recent_download(resolved_name, mod_id=mod_id, cfg=cfg)
    if recent and recent.exists():
        print(f"\n[Tier 3 · 本地包自动接管] 发现已下载的安装包: {recent.name}")
        return {"archive": recent, "version": resolved_ver, "mod_name": resolved_name, "source": "local_pickup"}

    # 4. 自动无缝唤起/复用桌面 Chrome 进行页面模拟下载
    if allow_browser_sim:
        arch = run_browser_simulation_download(
            game_domain=game_domain,
            mod_id=mod_id,
            file_id=main_fid,
            mod_name=resolved_name,
            cfg=cfg,
            close_browser=close_browser,
            visible=True,
            config_path=os.environ.get("MOD_CONFIG_PATH")
        )
        if arch and arch.exists():
            return {"archive": arch, "version": resolved_ver, "mod_name": resolved_name, "source": "browser_cdp"}

    return {"archive": None, "version": resolved_ver, "mod_name": resolved_name, "source": "none"}


def deploy_and_register_archive(
    engine: AdaptiveInstallEngine,
    arch: Path,
    game_domain: str,
    game_root: Path,
    mods_path: Path,
    mod_name: str,
    mod_id: Optional[int],
    mod_version: str,
    tmp_dir: Path,
    installed_list: List[Dict[str, Any]],
    with_translation: bool = False,
    download_source: str = "local_archive",
    original_query: str = "",
    search_term: str = "",
    nexus_game_id: Optional[int] = None
) -> Dict[str, Any]:
    """统一执行解压安装、前置校验、汉化叠加、专属清单登记并输出自包含的 [安装验证摘要]"""
    install_res = engine.install_mod_archive(arch, game_domain, game_root, mod_name)
    installed_dir = install_res["installed_dir"]
    strategy = install_res["strategy"]
    installed_files = install_res.get("installed_files", [])

    # 仅清理临时下载目录内的压缩包，不误删用户系统 Downloads 下的原始备份包
    try:
        clean_parents = {tmp_dir.resolve(), (PROJECT_ROOT / "tmp_downloads").resolve()}
        if arch.exists() and arch.resolve().parent in clean_parents:
            arch.unlink(missing_ok=True)
    except Exception:
        pass

    # 若安装目录内含 manifest.json，提取更精准的名称、UniqueID 与版本号
    uid = ""
    manifest_file = installed_dir / "manifest.json" if (installed_dir and installed_dir.is_dir()) else None
    if manifest_file and manifest_file.exists():
        try:
            from adaptive_installer import _load_json_flexible
            mdata = _load_json_flexible(manifest_file.read_text(encoding="utf-8-sig"))
            mod_name = mdata.get("Name") or mod_name
            uid = mdata.get("UniqueID") or mdata.get("uniqueID") or ""
            mver = mdata.get("Version")
            if isinstance(mver, dict):
                mod_version = f"{mver.get('MajorVersion', 0)}.{mver.get('MinorVersion', 0)}.{mver.get('PatchVersion', 0)}"
            elif mver:
                mod_version = str(mver)
            if not mod_id:
                for uk in mdata.get("UpdateKeys") or []:
                    m_uk = re.search(r"Nexus:\s*(\d+)", str(uk), re.I)
                    if m_uk:
                        mod_id = int(m_uk.group(1))
                        break
        except Exception:
            pass

    dep_check = {"satisfied": True, "missing": [], "installed_deps": []}
    if installed_dir and installed_dir.is_dir():
        dep_check = engine.check_dependencies(installed_dir, installed_list)
        if not dep_check["satisfied"]:
            print(f"\n[本地前置提示] 模组 [{mod_name}] 声明了以下未满足的本地依赖：")
            for mis in dep_check["missing"]:
                print(f"  - UniqueID: {mis['unique_id']} (最低版本: {mis.get('min_version') or '任意'})")

    i18n_applied_files: List[str] = []
    if with_translation and installed_dir and installed_dir.is_dir():
        trans_zip = create_or_fetch_translation(installed_dir, mod_name, tmp_dir)
        if trans_zip and trans_zip.exists():
            i18n_res = engine.apply_i18n_translation(installed_dir, trans_zip)
            i18n_applied_files = i18n_res.get("files_installed", [])
            print(f"  ✓ 中文汉化配置已合并至: {i18n_res['target_dir']} ({i18n_applied_files})")
            trans_zip.unlink(missing_ok=True)

    reg = engine.load_installed_registry(mods_path, game_domain=game_domain)
    if installed_dir and installed_dir != game_root and installed_dir != strategy["target_path"]:
        reg_key = installed_dir.name
    else:
        reg_key = sanitize_name(mod_name)

    if game_domain == "stardewvalley":
        zh_name = get_stardew_chinese_name(mod_name, reg_key, uid or reg_key.lower())
    else:
        zh_name = get_mod_translations(game_domain).get("mod_names", {}).get(reg_key, "")

    final_dest = installed_dir or strategy["target_path"]
    reg[reg_key] = {
        "name": mod_name,
        "chinese_name": zh_name,
        "name_zh": zh_name,
        "unique_id": uid or reg_key.lower(),
        "version": str(mod_version or "最新"),
        "nexus_id": mod_id,
        "format": strategy["mode"],
        "path": str(final_dest).replace("\\", "/"),
        "installed_files": installed_files,
        "status": "installed"
    }
    engine.save_installed_registry(mods_path, reg, game_domain=game_domain)
    registry_file = engine.get_installed_registry_path(mods_path, game_domain=game_domain)

    # 学习闭环：安装成功后自动沉淀新游戏部署规则与中文搜索意图映射
    learned_notes: List[str] = []
    try:
        learned_prof = engine.learn_from_install(
            game_domain=game_domain,
            game_root=game_root,
            strategy=strategy,
            mod_name=mod_name,
            nexus_game_id=nexus_game_id
        )
        if learned_prof:
            learned_notes.append(f"规则已记忆({learned_prof.get('mods_subdir')} / {learned_prof.get('install_mode')})")
    except Exception:
        pass
    try:
        if original_query:
            if learn_search_keyword(game_domain, original_query, search_term or mod_name, mod_name=mod_name):
                learned_notes.append(f"中文词条已沉淀({original_query} -> {search_term or mod_name})")
    except Exception:
        pass

    # 统计顶层落盘文件结构，供调用方直接确认无双层嵌套，省去额外的 Get-ChildItem 复查
    top_entries = []
    if final_dest.exists() and final_dest.is_dir():
        try:
            top_entries = [p.name + ("/" if p.is_dir() else "") for p in sorted(final_dest.iterdir(), key=lambda x: x.name)[:8]]
        except Exception:
            pass
    active_total = sum(1 for v in reg.values() if isinstance(v, dict) and v.get("status") != "uninstalled")
    missing_summary = "全部已就绪" if dep_check["satisfied"] else f"缺失 {[m['unique_id'] for m in dep_check['missing']]}"

    print("\n[安装验证摘要]")
    print(f"  ├─ 模组信息: {mod_name}" + (f" [{zh_name}]" if zh_name and zh_name != mod_name else "") + f" | 版本: v{mod_version} | Nexus ID: {mod_id} | 通道: {download_source}")
    print(f"  ├─ 物理路径: {final_dest} (策略: {strategy['mode']}, 顶层结构: {top_entries})")
    if with_translation:
        print(f"  ├─ 汉化状态: {'已合并 ' + str(i18n_applied_files) if i18n_applied_files else '当前模组无预置附加汉化包'}")
    print(f"  ├─ 前置依赖: {missing_summary}")
    if learned_notes:
        print(f"  ├─ 学习沉淀: {'; '.join(learned_notes)} -> mod_mappings.json")
    print(f"  └─ 专属清单: {registry_file} (当前活跃模组共 {active_total} 个)")
    return reg[reg_key]


def create_or_fetch_translation(installed_dir: Path, mod_name: str, tmp_dir: Path) -> Optional[Path]:
    trans_zip = tmp_dir / f"{sanitize_name(mod_name)}_i18n_cn.zip"
    if "skip" in mod_name.lower() and "fishing" in mod_name.lower():
        i18n_data = {
            "config.skip-minigame.name": "跳过钓鱼小游戏",
            "config.skip-minigame.desc": "咬钩后直接判定捕获，免玩拉线小游戏",
            "config.auto-fish.name": "自动钓鱼",
            "config.auto-fish.desc": "抛竿后鱼咬钩自动提竿",
            "config.pause-time.name": "钓鱼时暂停时间",
            "config.pause-time.desc": "钓鱼过程中游戏内时间停止流动"
        }
        with zipfile.ZipFile(trans_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("i18n/default.json", json.dumps(i18n_data, indent=2, ensure_ascii=False))
            zf.writestr("i18n/zh.json", json.dumps(i18n_data, indent=2, ensure_ascii=False))
        return trans_zip
    return None


def check_and_update_mods(
    api: NexusAPI,
    game_domain: str,
    game_root: Path,
    mods_path: Path,
    engine: AdaptiveInstallEngine,
    tmp_dir: Path,
    cfg: dict,
    non_interactive: bool = False,
    allow_browser_sim: bool = True
):
    print("\n" + "=" * 60)
    print(f" 🔄 检查 [{game_domain}] 已安装模组更新...")
    print("=" * 60)

    registry = engine.load_installed_registry(mods_path, game_domain=game_domain)
    updatable = []

    for fkey, item in registry.items():
        if item.get("status") == "uninstalled":
            continue
        nexus_id = item.get("nexus_id")
        current_ver = str(item.get("version") or "").strip()
        if not nexus_id:
            continue
        files = api.get_mod_files(game_domain, int(nexus_id), category="main")
        if not files:
            continue
        main_file = max(files, key=lambda f: f.get("uploaded_timestamp", 0))
        remote_ver = str(main_file.get("version") or "").strip()
        if remote_ver and current_ver and remote_ver != current_ver and current_ver not in ("未知", "最新"):
            updatable.append({
                "fkey": fkey,
                "item": item,
                "remote_file": main_file,
                "current_ver": current_ver,
                "new_ver": remote_ver
            })

    if not updatable:
        print("✓ 所有已关联 Nexus ID 的活跃模组均为最新版本！")
        return

    print(f"\n发现 {len(updatable)} 个模组存在新版本：")
    for i, u in enumerate(updatable, 1):
        print(f"  {i}. {u['item']['name']} (当前: {u['current_ver']} -> 最新: {u['new_ver']})")

    if not non_interactive:
        ans = input("\n是否立即更新全部可升级模组？[Y/n] ").strip().lower()
        if ans == "n":
            return

    for u in updatable:
        item = u["item"]
        name = item["name"]
        nid = int(item["nexus_id"])
        fid = u["remote_file"].get("file_id")
        print(f"\n[升级中] {name} ({u['current_ver']} -> {u['new_ver']}) ...")
        acq = acquire_mod_archive(
            api=api,
            game_domain=game_domain,
            mod_id=nid,
            mod_name=name,
            tmp_dir=tmp_dir,
            cfg=cfg,
            file_id=fid,
            allow_browser_sim=allow_browser_sim
        )
        arch = acq.get("archive")
        if not arch or not arch.exists():
            print(f"  [跳过] 未能获取 [{name}] 更新安装包")
            continue
        deploy_and_register_archive(
            engine=engine,
            arch=arch,
            game_domain=game_domain,
            game_root=game_root,
            mods_path=mods_path,
            mod_name=name,
            mod_id=nid,
            mod_version=u["new_ver"],
            tmp_dir=tmp_dir,
            installed_list=list(registry.values())
        )


def display_grouped_installed_mods(engine: AdaptiveInstallEngine, mods_path: Optional[Path] = None, target_game: Optional[str] = None, cfg: Optional[dict] = None) -> None:
    if target_game:
        target_reg = engine.load_installed_registry(mods_path, game_domain=target_game)
        grouped = {target_game: target_reg}
    else:
        grouped = engine.get_full_grouped_registry(mods_path)
    if not grouped:
        print("\n[清单提示] 当前暂无任何已登记的模组记录。")
        return

    print("\n" + "=" * 70)
    print(" 🎮 本地模组清单")
    print("=" * 70)

    cfg_games = (cfg or {}).get("games", {})
    for gdomain, mods in grouped.items():
        if target_game and gdomain != target_game:
            continue
        if not isinstance(mods, dict):
            continue
        ginfo = cfg_games.get(gdomain, {})
        gname = ginfo.get("name") or engine.rules.get(gdomain, {}).get("name") or gdomain
        gpath = ginfo.get("modsPath") or (str(mods_path) if mods_path and gdomain == target_game else "已配置路径")

        installed = [m for m in mods.values() if m.get("status") != "uninstalled"]
        uninstalled = [m for m in mods.values() if m.get("status") == "uninstalled"]
        reg_file = engine.get_installed_registry_path(mods_path, game_domain=gdomain)

        print(f"\n▶ 游戏: {gname} [{gdomain}]")
        print(f"  ├─ 模组路径: {gpath}")
        print(f"  ├─ 专属清单: {reg_file}")
        print(f"  └─ 状态统计: 已安装 {len(installed)} 个 | 已归档卸载 {len(uninstalled)} 个")

        for idx, m in enumerate(installed[:50], 1):
            nid_str = f"[Nexus ID: {m.get('nexus_id')}]" if m.get("nexus_id") else "[无Nexus关联]"
            ver_str = f"(v{m.get('version', '未知')})"
            zh_name = m.get("chinese_name") or m.get("name_zh")
            disp = f"{m.get('name')} [{zh_name}]" if zh_name and zh_name != m.get("name") else m.get("name")
            print(f"     {idx:2d}. {disp} {ver_str} {nid_str} 格式:{m.get('format', '未知')}")
        if len(installed) > 50:
            print(f"     ... 以及其他 {len(installed) - 50} 个模组")
    print("=" * 70 + "\n")


def display_grouped_uninstalled_mods(engine: AdaptiveInstallEngine, mods_path: Optional[Path] = None, target_game: Optional[str] = None, cfg: Optional[dict] = None) -> None:
    un_list = engine.list_uninstalled_mods(mods_path, game_domain=target_game)
    print("\n" + "=" * 70)
    print(" 📦 已卸载/归档模组历史记录")
    print("=" * 70)
    if not un_list:
        print("  (暂无已归档卸载的模组)")
        print("=" * 70 + "\n")
        return
    for idx, m in enumerate(un_list, 1):
        gd = m.get("game_domain") or target_game or "stardewvalley"
        nid_tag = f"[Nexus ID: {m.get('nexus_id')}]" if m.get("nexus_id") else "[无Nexus关联]"
        print(f"  {idx:2d}. [{gd}] {m.get('name')} (v{m.get('version', '未知')}) {nid_tag} | 卸载于: {m.get('uninstalled_at', '未知')}")
    print("=" * 70 + "\n")


def print_manual_fallback_guide(game_domain: str, mod_id: int, mod_name: str):
    web_url = f"https://www.nexusmods.com/{get_nexus_domain(game_domain)}/mods/{mod_id}?tab=files"
    print("\n" + "=" * 65)
    print(" ⚠️ 未能自动获取模组安装包，可使用以下任一兜底方式：")
    print("=" * 65)
    print(f"目标模组: {mod_name} (Nexus ID: {mod_id})")
    print(f"🔗 网页地址: {web_url}")
    print(f"  1. 桌面浏览器模拟直装:")
    print(f"     python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py -g {game_domain} --mod-id {mod_id} --simulate-browser")
    print(f"  2. NXM 协议直链安装 (网页点击 Mod Manager Download 复制链接):")
    print(f"     python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py -g {game_domain} --nxm \"<nxm://链接>\"")
    print(f"  3. 本地压缩包直装:")
    print(f"     python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py -g {game_domain} --install-zip \"<压缩包路径>\" --mod-id {mod_id}")
    print("=" * 65 + "\n")


def main():
    ap = argparse.ArgumentParser(description="Nexus 模组搜索、四级自动下载与自适应安装工具")
    ap.add_argument("-c", "--config", help="自定义 mod_config.json 路径")
    ap.add_argument("-k", "--api-key", help="Nexus API Key")
    ap.add_argument("-g", "--game", dest="game_domain", help="目标游戏（如 stardewvalley / cyberpunk2077 / rdr2 / pbz）")
    ap.add_argument("-m", "--mods-path", help="自定义 Mods 目录路径")
    ap.add_argument("-q", "--query", help="搜索关键词（支持中文功能词、模组名或直接传入 Nexus 链接）")
    ap.add_argument("-id", "--mod-id", type=int, default=None, help="直接按 Nexus Mod ID 下载并安装（免搜索）")
    ap.add_argument("-f", "--file-id", type=int, default=None, help="指定 Nexus File ID（配合 --mod-id 使用）")
    ap.add_argument("--mod-name", default="", help="配合 --install-zip 或 --mod-id 指定模组展示名称")
    ap.add_argument("--mod-version", default="", help="配合 --install-zip 指定模组版本号")
    ap.add_argument("--url", help="Nexus 模组网页链接、GitHub Release 直链或 nxm:// 链接")
    ap.add_argument("--nxm", help="Nexus Mod Manager 协议链接 (nxm://...)")
    ap.add_argument("--simulate-browser", "--chrome-devtools", dest="simulate_browser", action="store_true", default=True, help="免费账号遇到 403 时自动无缝复用桌面 Chrome 模拟下载（默认开启）")
    ap.add_argument("--no-simulate-browser", dest="simulate_browser", action="store_false", help="禁用自动浏览器模拟下载，遇到 403 仅提示链接")
    ap.add_argument("--close-browser", action="store_true", help="浏览器模拟下载完成后自动关闭专用 Chrome 窗口（默认保留热会话）")
    ap.add_argument("--install-zip", help="直接安装本地已有的模组压缩包 (.zip / .7z / .rar)")
    ap.add_argument("--skip-scan", action="store_true", help="跳过前置全目录扫描，直接读取现有专属清单以加速安装")
    ap.add_argument("--with-translation", action="store_true", help="安装时尝试自动合并中文汉化")
    ap.add_argument("--download-deps", dest="download_deps", action="store_true", default=True, help="自动检测并联动下载缺失的必要前置模组（默认开启）")
    ap.add_argument("--no-download-deps", dest="download_deps", action="store_false", help="仅提示前置依赖，不自动下载前置")
    ap.add_argument("--check-updates", action="store_true", help="检查单游戏已安装模组更新并支持一键升级")
    ap.add_argument("--scan-only", action="store_true", help="仅扫描目标游戏并更新其专属清单")
    ap.add_argument("--list", action="store_true", help="展示模组清单（传 -g 时仅展示单游戏）")
    ap.add_argument("--list-uninstalled", action="store_true", help="列出已归档卸载的历史模组")
    ap.add_argument("--uninstall", help="软删除卸载指定模组（移入 deleted_mods 安全归档）")
    ap.add_argument("--reinstall", help="从归档中一键恢复已卸载模组")
    ap.add_argument("--hard-delete", action="store_true", help="卸载时永久物理删除（不归档）")
    ap.add_argument("--i18n-apply", help="对已安装模组独立合并汉化包压缩文件")
    ap.add_argument("--target-mod", help="配合 --i18n-apply 使用的目标模组目录名")
    ap.add_argument("--init-config", "--detect-games", action="store_true", dest="init_config", help="初始化项目配置与 MCP 配置")
    ap.add_argument("--force-rescan", action="store_true", help="配合 --init-config 强制重新探测本地游戏")
    ap.add_argument("--add-game", help="显式授权并按需探测添加新游戏至 mod_config.json 白名单")
    ap.add_argument("--game-path", help="配合 --add-game 指定自定义游戏根目录")
    ap.add_argument("--show-learned", action="store_true", help="查看 mod_mappings.json 中已沉淀的游戏规则与别名总览")
    ap.add_argument("--learn-rule", action="store_true", help="手动录入/覆盖指定游戏的自适应安装规则至 mod_mappings.json（配合 -g / --mods-subdir / --install-mode / --framework / --alias）")
    ap.add_argument("--mods-subdir", help="配合 --learn-rule 指定相对模组目录（如 BepInEx/plugins）")
    ap.add_argument("--install-mode", help="配合 --learn-rule 指定部署模式（subfolder_per_mod / merge_root / pak_folder / witcher3_mod / bepinex 等）")
    ap.add_argument("--framework", help="配合 --learn-rule 指定引擎框架标识（如 BepInEx / UnrealPak / REFramework / REDengine3）")
    ap.add_argument("--alias", help="配合 --learn-rule 登记游戏中文别名")
    ap.add_argument("--tmp-dir", help="临时下载目录")
    ap.add_argument("--non-interactive", action="store_true", help="非交互自动化执行模式")
    ap.add_argument("--select-index", type=int, default=None, help="搜索结果选取序号 (1-based)")
    args = ap.parse_args()

    from game_detector import init_mod_config, ensure_game_in_config

    if args.config:
        os.environ["MOD_CONFIG_PATH"] = str(Path(args.config).resolve())

    cfg_target = Path(args.config) if args.config else PROJECT_CONFIG

    if args.show_learned:
        mappings = load_mappings()
        summary = {
            "game_profiles": mappings.get("game_profiles", {}),
            "game_aliases": mappings.get("game_aliases", {}),
            "search_keywords_games": {k: len(v) for k, v in mappings.get("search_keywords", {}).items() if isinstance(v, dict)}
        }
        print("\n[统一映射与学习规则总览 (mod_mappings.json)]")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    if args.learn_rule:
        if not args.game_domain:
            print("[错误] --learn-rule 必须配合 -g <游戏域名> 使用。")
            sys.exit(1)
        dom = normalize_game_domain(args.game_domain)
        eng = AdaptiveInstallEngine()
        rule_payload: Dict[str, Any] = {"learned_from": "manual"}
        if args.mods_subdir:
            rule_payload["mods_subdir"] = args.mods_subdir
        if args.install_mode:
            rule_payload["install_mode"] = args.install_mode
        if args.framework:
            rule_payload["framework"] = args.framework
        saved = eng.save_learned_rule(dom, rule_payload)
        if args.alias:
            learn_game_alias(args.alias, dom)
        print(f"✓ 已写入游戏 [{dom}] 学习规则至 mod_mappings.json:")
        print(json.dumps(saved, indent=2, ensure_ascii=False))
        return

    if args.add_game:
        print(f"\n[授权添加游戏] 正在扫描并添加游戏: {args.add_game} ...")
        res = ensure_game_in_config(cfg_target, args.add_game, custom_path=args.game_path)
        if res:
            print(f"✓ 成功将游戏 [{res['name']}] ({res['gameDomain']}) 登记入 mod_config.json 白名单！")
            print(f"  - 游戏根目录: {res['gamePath']}")
            print(f"  - 模组目录  : {res['modsPath']}")
        else:
            print(f"✗ 本地未自动探测到游戏 [{args.add_game}]，请通过 --game-path 指定游戏安装目录。")
            sys.exit(1)
        return

    if args.init_config:
        new_cfg = init_mod_config(cfg_target, force=args.force_rescan)
        active_cnt = len(new_cfg.get("games", {}))
        print(f"✓ 项目环境初始化完成！当前白名单共 {active_cnt} 款游戏 (默认: {new_cfg.get('defaultGame')})。")
        return

    cfg = load_config(args.config)
    if not cfg or not cfg.get("games"):
        cfg = init_mod_config(PROJECT_CONFIG, force=False)
    elif not (PROJECT_ROOT / ".mcp.json").exists() or not (PROJECT_ROOT / "mcp_config.json").exists():
        from config_manager import init_project_environment
        cfg = init_project_environment(cfg, project_root=PROJECT_ROOT, save_mcp=True, save_cfg=True)

    # 识别是否传入了 Nexus 网页链接或 NXM 链接（自动提取 game_domain / mod_id / file_id）
    parsed_ref = (
        parse_nexus_reference(args.nxm or "") or
        parse_nexus_reference(args.url or "") or
        parse_nexus_reference(args.query or "")
    )
    if parsed_ref:
        if parsed_ref.get("mod_id") and not args.mod_id:
            args.mod_id = parsed_ref["mod_id"]
        if parsed_ref.get("file_id") and not args.file_id:
            args.file_id = parsed_ref["file_id"]
        if parsed_ref["type"] == "web" and args.url and "nexusmods.com" in args.url.lower():
            args.url = None  # 交由四级下载管道处理，防止直接把网页 HTML 当压缩包下载
        if parsed_ref["type"] == "web" and args.query and "nexusmods.com" in args.query.lower():
            args.query = None

    raw_game = args.game_domain or (parsed_ref.get("game_domain") if parsed_ref else None)
    game_domain = normalize_game_domain(raw_game) if raw_game else normalize_game_domain(cfg.get("defaultGame") or "stardewvalley")

    # 严格执行游戏白名单校验，绝不隐式添加未经授权的游戏
    allowed_games = cfg.get("games", {})
    if game_domain not in allowed_games and not args.mods_path:
        disp_pair = f"{raw_game} -> {game_domain}" if (raw_game and raw_game.strip().lower() != game_domain) else game_domain
        print(f"[白名单拦截] 游戏 [{disp_pair}] 不在 mod_config.json 的已授权白名单中！(已完成中英文域名归一化，无需更换英文名重试)")
        print(f"当前已授权白名单游戏仅限: {list(allowed_games.keys())}")
        print(f"如用户明确授权添加该游戏，可执行: python .agents/skills/nexus-mod-install/scripts/nexus_mod_install.py --add-game \"{game_domain}\"")
        sys.exit(1)

    mods_path = resolve_mods_path(args.mods_path, game_domain, cfg)
    game_root = resolve_game_root(game_domain, cfg, mods_path)
    tmp_dir = resolve_tmp_dir(args.tmp_dir, cfg)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    engine = AdaptiveInstallEngine()

    # 查看清单与卸载历史仅读取项目 JSON，即使游戏所在移动硬盘未连接也正常展示
    if args.list:
        display_grouped_installed_mods(engine, mods_path=mods_path, target_game=(game_domain if args.game_domain else None), cfg=cfg)
        return

    if args.list_uninstalled:
        display_grouped_uninstalled_mods(engine, mods_path=mods_path, target_game=(game_domain if args.game_domain else None), cfg=cfg)
        return

    # 校验游戏物理根目录是否已挂载就绪（防止移动硬盘未插时误创建空目录或报错崩溃）
    if not args.mods_path and not game_root.exists():
        print(f"✗ [磁盘未挂载] 游戏 [{game_domain}] 的安装目录当前不可访问: {game_root}")
        print("  请先连接游戏所在磁盘/移动硬盘，或检查 mod_config.json 中的 gamePath 配置后再执行安装/卸载/扫描操作。")
        sys.exit(1)

    mods_path.mkdir(parents=True, exist_ok=True)

    # 卸载 / 恢复 / 查看清单 / 独立汉化
    if args.uninstall:
        target_term = translate_mod_query(args.uninstall, game_domain)
        un_res = engine.uninstall_mod(mods_path, args.uninstall, hard_delete=args.hard_delete, game_domain=game_domain)
        if not un_res["success"] and target_term != args.uninstall:
            un_res = engine.uninstall_mod(mods_path, target_term, hard_delete=args.hard_delete, game_domain=game_domain)
        if un_res["success"]:
            print(f"✓ 模组 [{un_res['mod_name']}] 已从 [{game_domain}] 卸载并归档至: {un_res.get('backup_path')}")
        else:
            print(f"✗ 卸载失败: {un_res['error']}")
            sys.exit(1)
        return

    if args.reinstall:
        target_term = translate_mod_query(args.reinstall, game_domain)
        re_res = engine.reinstall_mod(mods_path, args.reinstall, game_domain=game_domain)
        if not re_res["success"] and target_term != args.reinstall:
            re_res = engine.reinstall_mod(mods_path, target_term, game_domain=game_domain)
        if re_res["success"]:
            print(f"✓ 模组 [{re_res['mod_name']}] 已成功恢复至 [{game_domain}]！")
        else:
            print(f"✗ 恢复失败: {re_res['error']}")
            sys.exit(1)
        return

    if args.i18n_apply:
        i18n_zip = Path(args.i18n_apply)
        if not i18n_zip.exists():
            print(f"[错误] 汉化包不存在: {i18n_zip}")
            sys.exit(1)
        if not args.target_mod:
            print("[错误] 请通过 --target-mod 指定要打汉化的模组文件夹名称")
            sys.exit(1)
        res = engine.apply_i18n_translation(mods_path / args.target_mod, i18n_zip)
        print(f"✓ 汉化文件已合并至: {res['target_dir']} ({res['files_installed']})")
        return

    api_key = resolve_api_key(args.api_key, cfg)
    api = NexusAPI(api_key) if (api_key and HAS_REQUESTS) else None

    # 加载或扫描当前单游戏清单
    if args.skip_scan:
        reg_map = engine.load_installed_registry(mods_path, game_domain=game_domain)
        installed_list = [v for v in reg_map.values() if isinstance(v, dict) and v.get("status") != "uninstalled"]
    else:
        print(f"\n[单游戏校准] 正在同步 [{game_domain}] 本地模组状态...")
        installed_list = engine.scan_installed_mods(mods_path, game_domain)
        print(f"✓ 当前已安装活跃模组: {len(installed_list)} 个")

    if args.scan_only:
        display_grouped_installed_mods(engine, mods_path=mods_path, target_game=game_domain, cfg=cfg)
        return

    if args.check_updates:
        if not api:
            print("[错误] 检查更新需要有效的 Nexus API Key。")
            sys.exit(1)
        check_and_update_mods(
            api, game_domain, game_root, mods_path, engine, tmp_dir, cfg,
            non_interactive=args.non_interactive,
            allow_browser_sim=args.simulate_browser
        )
        return

    # 分支 A：本地压缩包直装
    if args.install_zip:
        arch = Path(args.install_zip)
        if not arch.exists():
            print(f"[错误] 指定的本地压缩包不存在: {arch}")
            sys.exit(1)
        inferred_id = args.mod_id
        if not inferred_id:
            m_id = re.search(r"-(\d{2,7})-", arch.stem)
            if m_id:
                inferred_id = int(m_id.group(1))
        m_name = args.mod_name or arch.stem
        m_ver = args.mod_version or "最新"
        deploy_and_register_archive(
            engine=engine,
            arch=arch,
            game_domain=game_domain,
            game_root=game_root,
            mods_path=mods_path,
            mod_name=m_name,
            mod_id=inferred_id,
            mod_version=m_ver,
            tmp_dir=tmp_dir,
            installed_list=installed_list,
            with_translation=args.with_translation
        )
        return

    # 分支 B：外部非 Nexus 直接下载链接 (如 GitHub Release zip 直链)
    if args.url and not parsed_ref:
        raw_fname = sanitize_name(unquote(Path(urlparse(args.url).path).name) or "downloaded_mod.zip")
        if not any(raw_fname.lower().endswith(ext) for ext in (".zip", ".7z", ".rar")):
            raw_fname += ".zip"
        arch = download_file(args.url, tmp_dir / raw_fname, label=args.mod_name or raw_fname)
        deploy_and_register_archive(
            engine=engine,
            arch=arch,
            game_domain=game_domain,
            game_root=game_root,
            mods_path=mods_path,
            mod_name=args.mod_name or arch.stem,
            mod_id=args.mod_id,
            mod_version=args.mod_version or "最新",
            tmp_dir=tmp_dir,
            installed_list=installed_list,
            with_translation=args.with_translation
        )
        return

    # 分支 C：已知 Mod ID（来自 --mod-id、Nexus 网页 URL 或 NXM 协议链接）或关键词搜索
    target_mod_id = args.mod_id
    target_file_id = args.file_id
    target_mod_name = args.mod_name
    target_mod_ver = args.mod_version or "最新"
    nxm_key = parsed_ref.get("key") if (parsed_ref and parsed_ref["type"] == "nxm") else None
    nxm_expires = parsed_ref.get("expires") if (parsed_ref and parsed_ref["type"] == "nxm") else None
    original_query_text = ""
    resolved_search_term = ""

    gid = engine.rules.get(game_domain, {}).get("nexus_game_id")
    if not gid and api:
        gid = api.get_game_id(game_domain)

    if not target_mod_id:
        query = args.query
        if not query:
            if args.non_interactive:
                print("[提示] 未提供 --query / --mod-id / --url / --install-zip 参数，退出。")
                return
            query = input(f"\n请输入要为 [{game_domain}] 搜索安装的模组名或中文功能描述: ").strip()
            if not query:
                return

        original_query_text = query
        search_term = translate_mod_query(query, game_domain)
        resolved_search_term = search_term
        if search_term != query:
            print(f"\n[语义映射] 中文意图 [{query}] -> Nexus 最佳检索词: [{search_term}]")

        # 若无 API Key，尝试直接匹配开源镜像
        if not api:
            mirror_only = resolve_mirror_download(search_term, game_domain=game_domain)
            if mirror_only:
                arch = download_file(mirror_only["url"], tmp_dir / mirror_only["filename"], label=mirror_only["name"])
                deploy_and_register_archive(
                    engine, arch, game_domain, game_root, mods_path,
                    mirror_only["name"], mirror_only.get("mod_id"), mirror_only.get("version", "最新"),
                    tmp_dir, installed_list, with_translation=args.with_translation
                )
                return
            print("[错误] 未配置有效 Nexus API Key，无法执行在线搜索。")
            sys.exit(1)

        print(f"\n正在检索 Nexus Mods ({game_domain}): {search_term} ...")
        results = api.search_mods(search_term, game_id=gid or 1303, game_domain=game_domain)
        if not results and search_term != query:
            results = api.search_mods(query, game_id=gid or 1303, game_domain=game_domain)

        if not results:
            print(f"✗ 未找到匹配 [{query}] 的模组。")
            sys.exit(1)

        print("\n搜索候选列表（按下载量热度排序）：")
        for i, r in enumerate(results, 1):
            dl_cnt = r.get("mod_unique_downloads", 0)
            print(f"  [{i}] {r.get('name')} | Nexus ID: {r.get('mod_id')} | 版本: {r.get('version', '未知')} | 下载量: {dl_cnt:,}")

        if args.select_index is not None and 1 <= args.select_index <= len(results):
            selected = results[args.select_index - 1]
        elif args.non_interactive:
            selected = results[0]
        else:
            while True:
                c = input(f"请选择要安装的序号 [1-{len(results)}]，输入 0 取消: ").strip()
                if c == "0":
                    return
                if c.isdigit() and 1 <= int(c) <= len(results):
                    selected = results[int(c) - 1]
                    break

        target_mod_id = int(selected["mod_id"])
        target_mod_name = selected.get("name") or f"Mod_{target_mod_id}"
        target_mod_ver = selected.get("version") or target_mod_ver

    if not target_mod_name and api:
        minfo = api.get_mod_info(game_domain, target_mod_id)
        target_mod_name = minfo.get("name") or f"Mod_{target_mod_id}"
        target_mod_ver = minfo.get("version") or target_mod_ver
    target_mod_name = target_mod_name or f"Mod_{target_mod_id}"

    print(f"\n🎯 目标锁定: {target_mod_name} (Nexus ID: {target_mod_id}, 游戏: {game_domain})")

    # 1. 前置依赖联查与自动预装（复用同一下载管道与热浏览器会话）
    if api and gid:
        req_info = api.get_mod_requirements(target_mod_id, gid)
        nexus_reqs = [r for r in req_info.get("requirements", []) if not r.get("externalRequirement")]
        if nexus_reqs:
            print(f"\n[前置依赖联查] 模组声明了 {len(nexus_reqs)} 项 Nexus 平台依赖：")
            for r in nexus_reqs:
                r_name = r.get("modName") or f"Mod_{r.get('modId')}"
                r_id = int(r["modId"]) if str(r.get("modId", "")).isdigit() else None
                r_note = (r.get("notes") or "").strip()
                # 跳过翻译类/可选补丁类反向说明或已安装项
                is_installed = any(
                    (r_id and str(item.get("nexus_id")) == str(r_id)) or
                    r_name.lower() in (item.get("name") or "").lower() or
                    (r_name.lower().replace(" ", "") in (item.get("folder") or "").lower().replace(" ", ""))
                    for item in installed_list
                )
                # SMAPI 在游戏根目录而非 Mods 子目录时视为已就绪
                if r_id == 2400 and (game_root / "StardewModdingAPI.exe").exists():
                    is_installed = True
                if r_name.lower() == "cyber engine tweaks" and (game_root / "bin" / "x64" / "plugins" / "cyber_engine_tweaks.asi").exists():
                    is_installed = True

                if is_installed:
                    print(f"  ✓ [已安装] {r_name} (ID: {r_id})")
                else:
                    note_str = f" ({r_note})" if r_note else ""
                    print(f"  ! [缺失前置] {r_name} (ID: {r_id}){note_str}")
                    if args.download_deps and r_id and r_id != 2400:
                        print(f"    -> [自动联装前置] 正在获取前置依赖 [{r_name}] ...")
                        dep_acq = acquire_mod_archive(
                            api=api,
                            game_domain=game_domain,
                            mod_id=r_id,
                            mod_name=r_name,
                            tmp_dir=tmp_dir,
                            cfg=cfg,
                            allow_browser_sim=args.simulate_browser,
                            close_browser=False
                        )
                        dep_arch = dep_acq.get("archive")
                        if dep_arch and dep_arch.exists():
                            dep_entry = deploy_and_register_archive(
                                engine=engine,
                                arch=dep_arch,
                                game_domain=game_domain,
                                game_root=game_root,
                                mods_path=mods_path,
                                mod_name=dep_acq.get("mod_name") or r_name,
                                mod_id=r_id,
                                mod_version=dep_acq.get("version") or "最新",
                                tmp_dir=tmp_dir,
                                installed_list=installed_list
                            )
                            installed_list.append(dep_entry)

    # 2. 四级管道获取主模组安装包
    acq = acquire_mod_archive(
        api=api,
        game_domain=game_domain,
        mod_id=target_mod_id,
        mod_name=target_mod_name,
        tmp_dir=tmp_dir,
        cfg=cfg,
        file_id=target_file_id,
        nxm_key=nxm_key,
        nxm_expires=nxm_expires,
        allow_browser_sim=args.simulate_browser,
        close_browser=args.close_browser
    )

    main_arch = acq.get("archive")
    if not main_arch or not main_arch.exists():
        print_manual_fallback_guide(game_domain, target_mod_id, target_mod_name)
        sys.exit(1)

    # 3. 解压部署与专属清单登记
    deploy_and_register_archive(
        engine=engine,
        arch=main_arch,
        game_domain=game_domain,
        game_root=game_root,
        mods_path=mods_path,
        mod_name=acq.get("mod_name") or target_mod_name,
        mod_id=target_mod_id,
        mod_version=acq.get("version") or target_mod_ver,
        tmp_dir=tmp_dir,
        installed_list=installed_list,
        with_translation=args.with_translation,
        download_source=acq.get("source") or "auto",
        original_query=original_query_text,
        search_term=resolved_search_term,
        nexus_game_id=gid
    )


if __name__ == "__main__":
    main()
