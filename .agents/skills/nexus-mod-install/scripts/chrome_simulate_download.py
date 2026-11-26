import os
import sys
import time
import json
import argparse
import subprocess
import requests
from pathlib import Path
from urllib.parse import quote

try:
    import websocket
except ImportError:
    websocket = None

# 项目基础路径
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from adaptive_installer import AdaptiveInstallEngine, get_stardew_chinese_name
from config_manager import (
    load_config, get_system_paths, normalize_game_domain, get_nexus_domain,
    resolve_mods_path, resolve_game_root, get_mod_translations
)


def _sanitize_name(name: str) -> str:
    return "".join("_" if c in r'\/:*?"<>|' else c for c in name).strip()


class ChromeBrowserSimulator:
    """
    通过 Chrome DevTools Protocol (CDP) 驱动真实桌面 Chrome 浏览器：
    1. 优先复用已有 CDP 端口会话，下载结束后默认保持会话热就绪（免重复冷启动）。
    2. 在 Windows 可见模式下优先通过 launch_on_desktop.py 挂载至 WinSta0\\Default 用户可见桌面。
    3. 支持 Browser.downloadWillBegin / downloadProgress 事件精准追踪与多目录兜底。
    4. 遇到登录或人机验证时绝不尝试绕过，提示用户在可见窗口手动完成后自动继续。
    """
    def __init__(self, chrome_exe=None, profile_dir=None, visible=True, port=None, close_on_exit=False):
        cfg = load_config()
        paths = get_system_paths(cfg)
        self.chrome_exe = chrome_exe or paths["chrome_exe"]
        self.profile_dir = Path(profile_dir or paths["chrome_profile_dir"])
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.visible = visible
        self.port = port or paths["cdp_port"]
        self.close_on_exit = close_on_exit
        self.proc = None
        self.ws = None
        self.browser_ws = None
        self.msg_id = 0
        self.reused = False
        self.active_download_guid = None
        self.suggested_filename = ""
        self.download_completed = False

    def launch(self, initial_url: str = "about:blank"):
        if websocket is None:
            raise RuntimeError("缺少 websocket-client 依赖，请执行 pip install websocket-client")

        # 1. 优先复用已运行的 Chrome CDP 会话
        try:
            r = requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1.5)
            if r.status_code == 200:
                print(f"[Chrome 热复用] 检测到端口 {self.port} 已有就绪调试会话，直接复用免冷启动！")
                self.reused = True
                return
        except Exception:
            pass

        print(f"[Chrome 启动] 启动专用桌面浏览器 (端口 {self.port}, Profile: {self.profile_dir})...")
        launcher = SCRIPT_DIR / "launch_on_desktop.py"
        if os.name == "nt" and self.visible and launcher.exists():
            cmd_line = (
                f'"{self.chrome_exe}" '
                f'--remote-debugging-port={self.port} '
                f'--user-data-dir="{self.profile_dir}" '
                f'--no-first-run --no-default-browser-check '
                f'--disable-blink-features=AutomationControlled '
                f'--start-maximized --new-window "{initial_url}"'
            )
            subprocess.run([sys.executable, str(launcher), cmd_line], check=False)
        else:
            cmd = [
                self.chrome_exe,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-blink-features=AutomationControlled",
            ]
            if not self.visible:
                cmd.append("--headless=new")
            cmd.append(initial_url)
            self.proc = subprocess.Popen(cmd)

        for _ in range(30):
            time.sleep(0.4)
            try:
                r = requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1)
                if r.status_code == 200:
                    return
            except Exception:
                pass
        raise RuntimeError(f"未能连接到 Chrome CDP 端口 {self.port}，请确认 Chrome 路径正确。")

    def connect_page(self, target_url: str = ""):
        ver_info = requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=5).json()
        b_ws_url = ver_info.get("webSocketDebuggerUrl")
        if b_ws_url:
            try:
                self.browser_ws = websocket.create_connection(b_ws_url, timeout=5)
                self.browser_ws.settimeout(0.05)
            except Exception:
                self.browser_ws = None

        r = requests.get(f"http://127.0.0.1:{self.port}/json", timeout=5)
        tabs = [t for t in r.json() if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        chosen = next((t for t in tabs if "nexusmods.com" in (t.get("url") or "")), None) or (tabs[0] if tabs else None)
        if not chosen:
            new_ep = f"http://127.0.0.1:{self.port}/json/new"
            if target_url:
                new_ep += f"?{quote(target_url, safe='')}"
            try:
                r_new = requests.put(new_ep, timeout=5)
                if r_new.status_code != 200:
                    r_new = requests.get(new_ep, timeout=5)
            except Exception:
                r_new = requests.get(new_ep, timeout=5)
            ws_url = r_new.json()["webSocketDebuggerUrl"]
        else:
            ws_url = chosen["webSocketDebuggerUrl"]

        self.ws = websocket.create_connection(ws_url, timeout=10)

    def _drain_browser_events(self):
        if not self.browser_ws:
            return
        for _ in range(20):
            try:
                raw = self.browser_ws.recv()
                msg = json.loads(raw)
                method = msg.get("method")
                params = msg.get("params") or {}
                if method == "Browser.downloadWillBegin":
                    self.active_download_guid = params.get("guid")
                    self.suggested_filename = params.get("suggestedFilename") or ""
                    print(f"⬇️ [CDP 下载开始] 文件: {self.suggested_filename}")
                elif method == "Browser.downloadProgress":
                    if self.active_download_guid and params.get("guid") != self.active_download_guid:
                        continue
                    if params.get("state") == "completed":
                        self.download_completed = True
            except Exception:
                break

    def send_cdp(self, method, params=None):
        self.msg_id += 1
        payload = {"id": self.msg_id, "method": method, "params": params or {}}
        self.ws.send(json.dumps(payload))
        while True:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == self.msg_id:
                return resp.get("result", {})

    def evaluate(self, expr):
        res = self.send_cdp("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return res.get("result", {}).get("value")

    def navigate(self, url):
        self.send_cdp("Page.enable")
        self.send_cdp("Page.bringToFront")
        self.send_cdp("Page.navigate", {"url": url})

    def set_download_path(self, download_dir: Path):
        resolved = str(download_dir.resolve())
        if self.browser_ws:
            try:
                self.browser_ws.send(json.dumps({
                    "id": 9001,
                    "method": "Browser.setDownloadBehavior",
                    "params": {"behavior": "allow", "downloadPath": resolved, "eventsEnabled": True}
                }))
            except Exception:
                pass
        try:
            self.send_cdp("Page.enable")
            self.send_cdp("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": resolved})
        except Exception:
            pass

    def is_logged_in(self):
        check_script = """
        (() => {
            const cookies = document.cookie || '';
            const hasAuthCookie = cookies.includes('nexusmods_session') || cookies.includes('sid');
            const hasUserMenu = !!document.querySelector('.nav-user-menu, .profile-name, .section-content[href*="logout"], a[href*="users/myaccount"]');
            const hasLoginBtn = !!document.querySelector('a[href*="/auth/sign_in"], button[aria-label="Log in"]');
            const isGuest = (document.body ? document.body.innerText : '').includes('guest\\nMember');
            return !isGuest && (hasUserMenu || (hasAuthCookie && !hasLoginBtn));
        })()
        """
        return bool(self.evaluate(check_script))

    def close(self):
        for sock in (self.ws, self.browser_ws):
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        if self.close_on_exit and self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass


def simulate_nexus_download(
    game_domain: str,
    mod_id: int,
    file_id: int = None,
    mod_name: str = "",
    profile_dir: str = None,
    timeout: int = 150,
    visible: bool = True,
    close_browser: bool = False
) -> Path:
    """通过原生 Python CDP 模拟点击 Nexus 网页下载并捕获完成的压缩包"""
    cfg = load_config()
    paths = get_system_paths(cfg)
    tmp_dir = paths["tmp_downloads_dir"]
    downloads_dir = paths["downloads_dir"]
    all_dl_dirs = paths.get("all_download_dirs") or [tmp_dir, downloads_dir]
    tmp_dir.mkdir(parents=True, exist_ok=True)

    nexus_dom = get_nexus_domain(game_domain)
    if file_id:
        target_url = f"https://www.nexusmods.com/{nexus_dom}/mods/{mod_id}?tab=files&file_id={file_id}&nmm=0"
    else:
        target_url = f"https://www.nexusmods.com/{nexus_dom}/mods/{mod_id}?tab=files"

    print(f"\n========================================================")
    print(f" 🌐 原生 Python CDP 页面模拟下载器 (会话复用模式)")
    print(f" 目标游戏: {game_domain} | 模组 ID: {mod_id} | File ID: {file_id or 'auto'}")
    print(f" 访问页面: {target_url}")
    print(f"========================================================")

    def get_existing_archives():
        found = {}
        for folder in all_dl_dirs:
            if folder.exists():
                for ext in ("*.zip", "*.7z", "*.rar"):
                    for f in folder.glob(ext):
                        try:
                            found[str(f.resolve())] = f.stat().st_mtime
                        except Exception:
                            pass
        return found

    initial_files = get_existing_archives()
    simulator = ChromeBrowserSimulator(profile_dir=profile_dir, visible=visible, close_on_exit=close_browser)

    try:
        simulator.launch(initial_url=target_url)
        simulator.connect_page(target_url=target_url)
        simulator.set_download_path(tmp_dir)

        if simulator.reused:
            simulator.navigate(target_url)

        start_time = time.time()
        button_clicked = False
        prompted_gate = False
        click_attempts = 0

        click_script = """
        (() => {
            const url = window.location.href || '';
            const title = (document.title || '').toLowerCase();
            const bodyText = (document.body ? document.body.innerText : '').slice(0, 2000).toLowerCase();
            if (
                title.includes('just a moment') ||
                bodyText.includes('verify you are human') ||
                document.querySelector('iframe[src*="challenges.cloudflare.com"], .cf-turnstile')
            ) {
                return 'HUMAN_VERIFICATION_REQUIRED';
            }
            if (url.includes('/auth/sign_in') || window.location.host === 'users.nexusmods.com') {
                return 'ON_LOGIN_PAGE';
            }

            function findElements(root, matcher) {
                let found = [];
                function walk(node) {
                    if (!node) return;
                    if (matcher(node)) found.push(node);
                    if (node.shadowRoot) walk(node.shadowRoot);
                    if (node.children) {
                        for (let child of node.children) walk(child);
                    }
                }
                walk(root);
                return found;
            }

            const slowBtns = findElements(document.body, el => {
                const t = (el.innerText || '').trim().toLowerCase();
                return (el.tagName === 'BUTTON' || el.tagName === 'A') && t.includes('slow download');
            });
            if (slowBtns.length > 0) {
                const href = slowBtns[0].getAttribute('href') || '';
                if (href.includes('users.nexusmods.com')) {
                    slowBtns[0].click();
                    return 'ON_LOGIN_PAGE';
                }
                slowBtns[0].scrollIntoView({ block: 'center' });
                slowBtns[0].click();
                return 'CLICKED_SLOW_DOWNLOAD';
            }

            const dlBtns = findElements(document.body, el => {
                const t = (el.innerText || '').trim().toLowerCase();
                const href = el.getAttribute('href') || '';
                return (el.tagName === 'BUTTON' || el.tagName === 'A') &&
                       (t === 'download' || t.includes('manual download') || href.includes('tab=files&file_id='));
            });
            if (dlBtns.length > 0) {
                const preferred = dlBtns.find(b => (b.innerText || '').trim().toLowerCase() === 'download') ||
                                  dlBtns.find(b => (b.innerText || '').toLowerCase().includes('manual download')) ||
                                  dlBtns[0];
                preferred.scrollIntoView({ block: 'center' });
                preferred.click();
                return 'CLICKED_MODAL_DOWNLOAD';
            }
            return 'WAITING_FOR_BUTTONS';
        })()
        """

        while time.time() - start_time < timeout:
            time.sleep(1.5)
            simulator._drain_browser_events()

            # 优先匹配 CDP 事件返回的文件名
            if simulator.suggested_filename:
                for folder in all_dl_dirs:
                    exact = folder / simulator.suggested_filename
                    if exact.exists() and not Path(str(exact) + ".crdownload").exists():
                        if exact.stat().st_size > 100 and (simulator.download_completed or not simulator.active_download_guid):
                            print(f"\n🎉 捕获到新下载完成的模组包: {exact.name} ({exact.stat().st_size / 1024:.1f} KB)")
                            return exact

            for folder in all_dl_dirs:
                if not folder.exists():
                    continue
                for ext in ("*.zip", "*.7z", "*.rar"):
                    for f in folder.glob(ext):
                        if Path(str(f) + ".crdownload").exists():
                            continue
                        rp = str(f.resolve())
                        st = f.stat()
                        if st.st_size > 100 and (rp not in initial_files or st.st_mtime > initial_files[rp] + 0.5):
                            if simulator.download_completed or not simulator.active_download_guid:
                                print(f"\n🎉 捕获到新下载完成的模组包: {f.name} ({st.st_size / 1024:.1f} KB)")
                                return f

            if not button_clicked:
                action_res = simulator.evaluate(click_script)
                if action_res == "HUMAN_VERIFICATION_REQUIRED":
                    if not prompted_gate:
                        print("⏸️ [等待验证] 检测到站点验证页面，请在可见 Chrome 窗口手动完成后自动继续...")
                        prompted_gate = True
                elif action_res == "ON_LOGIN_PAGE":
                    if not prompted_gate:
                        print("🔑 [等待登录] 请在可见 Chrome 窗口完成 Nexus 登录（Profile 会持久保存会话）...")
                        prompted_gate = True
                elif action_res == "CLICKED_SLOW_DOWNLOAD":
                    print("✓ 成功点击 [Slow Download] 慢速下载按钮，等待 5 秒倒计时与落盘...")
                    button_clicked = True
                elif action_res == "CLICKED_MODAL_DOWNLOAD":
                    print("✓ 已点击文件页/前置弹窗 [Download] 按钮，推进至下载页...")
                    click_attempts += 1
                    if click_attempts >= 4 and file_id:
                        simulator.navigate(target_url)
                        click_attempts = 0

        print("\n[警告] 等待下载超时，未能捕获到新生成的压缩文件。")
        return None
    finally:
        simulator.close()


def main():
    parser = argparse.ArgumentParser(description="Chrome DevTools 原生 CDP 自动化模拟下载与自适应安装")
    parser.add_argument("-g", "--game", dest="game_domain", help="目标游戏 domain (如 stardewvalley, cyberpunk2077)")
    parser.add_argument("-m", "--mod-id", type=int, default=None, help="Nexus Mod ID")
    parser.add_argument("-f", "--file-id", type=int, default=None, help="Nexus File ID (可选)")
    parser.add_argument("--name", default="", help="模组名称")
    parser.add_argument("--version", default="最新", help="模组版本号")
    parser.add_argument("--profile-dir", default=None, help="指定挂载的 Chrome Profile 目录")
    parser.add_argument("--login", action="store_true", help="打开专用浏览器并进入 Nexus 登录页持久化保存 Cookie")
    parser.add_argument("--headless", action="store_true", help="使用无头浏览器模式")
    parser.add_argument("--close-browser", action="store_true", help="下载完成后关闭专用浏览器窗口")
    parser.add_argument("--download-only", action="store_true", help="仅下载不自动解压安装")
    parser.add_argument("--timeout", type=int, default=150, help="等待下载超时时间 (秒)")
    args = parser.parse_args()

    cfg = load_config()
    paths = get_system_paths(cfg)

    if args.login:
        target_profile = Path(args.profile_dir) if args.profile_dir else paths["chrome_profile_dir"]
        print(f"\n========================================================")
        print(f" 🔑 Nexus Mods 专用 Profile 登录授权向导")
        print(f"========================================================")
        sim = ChromeBrowserSimulator(profile_dir=target_profile, visible=True)
        try:
            sim.launch(initial_url="https://users.nexusmods.com/auth/sign_in")
            sim.connect_page("https://users.nexusmods.com/auth/sign_in")
            if sim.reused:
                sim.navigate("https://users.nexusmods.com/auth/sign_in")
            print("✓ 请在弹出的桌面 Chrome 窗口中完成登录（完成后将自动识别保存）...")
            for _ in range(60):
                time.sleep(3)
                if sim.is_logged_in():
                    print("\n🎉 检测到已成功登录 Nexus Mods！会话已安全持久化保存。")
                    return
            print("\n提示：向导等待结束，您仍可在窗口中继续登录，Profile 会自动保存。")
        finally:
            sim.close()
        return

    if not args.game_domain or not args.mod_id:
        parser.error("下载模式必须提供 -g/--game 和 -m/--mod-id 参数，或使用 --login 进行登录初始化。")

    game_domain = normalize_game_domain(args.game_domain)
    arch = simulate_nexus_download(
        game_domain=game_domain,
        mod_id=args.mod_id,
        file_id=args.file_id,
        mod_name=args.name,
        profile_dir=args.profile_dir,
        timeout=args.timeout,
        visible=not args.headless,
        close_browser=args.close_browser
    )

    if arch and arch.exists():
        if args.download_only:
            print(f"[DOWNLOADED_ARCHIVE] {arch}")
            return

        print(f"\n[自适应部署] 正在将模拟下载的文件部署至游戏 [{game_domain}] ...")
        mods_path = resolve_mods_path(None, game_domain, cfg)
        game_root = resolve_game_root(game_domain, cfg, mods_path)
        engine = AdaptiveInstallEngine()

        mod_name = args.name or arch.stem
        install_res = engine.install_mod_archive(arch, game_domain, game_root, mod_name)
        installed_dir = install_res["installed_dir"]
        strategy = install_res["strategy"]

        reg = engine.load_installed_registry(mods_path, game_domain=game_domain)
        reg_key = installed_dir.name if (installed_dir and installed_dir != game_root) else _sanitize_name(mod_name)
        zh_name = (
            get_stardew_chinese_name(mod_name, reg_key, reg_key.lower())
            if game_domain == "stardewvalley"
            else get_mod_translations(game_domain).get("mod_names", {}).get(reg_key, "")
        )
        reg[reg_key] = {
            "name": mod_name,
            "chinese_name": zh_name,
            "name_zh": zh_name,
            "unique_id": reg_key.lower(),
            "version": args.version or "最新",
            "nexus_id": args.mod_id,
            "format": strategy["mode"],
            "path": str(installed_dir or strategy["target_path"]).replace("\\", "/"),
            "installed_files": install_res.get("installed_files", []),
            "status": "installed"
        }
        engine.save_installed_registry(mods_path, reg, game_domain=game_domain)
        print(f"\n✓ 模组 [{mod_name}] 页面模拟下载与自适应部署成功！")
        print(f"  安装目录: {installed_dir or strategy['target_path']}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
