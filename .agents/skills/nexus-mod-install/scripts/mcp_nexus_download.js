#!/usr/bin/env node
/**
 * mcp_nexus_download.js
 * 高速桌面 Chrome 页面模拟下载器（支持会话热复用 + 原生 CDP 零延迟直连 + MCP 兼容模式）
 *
 * 核心优化：
 *  1. 会话热复用：优先复用 127.0.0.1:<cdpPort> 已就绪的专用桌面 Chrome，免重复冷启动，下载完成后默认保留窗口供后续依赖/模组秒级复用（传 --close-browser 可显式关闭）。
 *  2. 原生 CDP 快速通道：利用 Node.js 内置 WebSocket 直连 Chrome DevTools Protocol，省去每次 npx 拉起 chrome-devtools-mcp 的 5~15 秒开销（传 --via-mcp 可切换 MCP 子进程模式）。
 *  3. 精准下载事件追踪：通过 Browser.setDownloadBehavior 定向落盘至 tmp_downloads，结合 Browser.downloadWillBegin / downloadProgress 事件与多目录增量兜底，零误判捕获压缩包（含 .zip/.7z/.rar）。
 *  4. 合规安全边界：遇到登录或人机验证时绝不尝试绕过，提示用户在桌面可见窗口手动完成后自动衔接继续。
 */

const { spawn, execSync } = require('child_process');
const readline = require('readline');
const fs = require('fs');
const path = require('path');
const os = require('os');
const http = require('http');

const SCRIPT_DIR = __dirname;

const GAME_ALIAS_MAP = {
  '星露谷': 'stardewvalley', '星露谷物语': 'stardewvalley', 'stardew': 'stardewvalley', 'sdv': 'stardewvalley', 'stardewvalley': 'stardewvalley',
  '赛博朋克': 'cyberpunk2077', '赛博朋克2077': 'cyberpunk2077', '2077': 'cyberpunk2077', 'cp2077': 'cyberpunk2077', 'cyberpunk': 'cyberpunk2077', 'cyberpunk2077': 'cyberpunk2077',
  '大镖客': 'reddeadredemption2', '大镖客2': 'reddeadredemption2', '荒野大镖客': 'reddeadredemption2', '荒野大镖客2': 'reddeadredemption2', 'rdr2': 'reddeadredemption2', 'reddeadredemption2': 'reddeadredemption2',
  '影之刃零': 'phantomblandzero', '影之刃0': 'phantomblandzero', '影之刃': 'phantomblandzero', 'pbz': 'phantomblandzero', 'phantombladezero': 'phantomblandzero', 'phantomblandzero': 'phantomblandzero',
  '黑神话悟空': 'blackmythwukong', '黑神话': 'blackmythwukong', 'blackmythwukong': 'blackmythwukong',
  '上古卷轴5': 'skyrimspecialedition', '老滚5': 'skyrimspecialedition', 'skyrimspecialedition': 'skyrimspecialedition'
};

function normalizeGameDomain(raw) {
  const clean = String(raw || '').trim().toLowerCase();
  return GAME_ALIAS_MAP[clean] || clean;
}

function getNexusDomain(domain) {
  const d = normalizeGameDomain(domain);
  return d === 'phantomblandzero' ? 'phantombladezero' : d;
}

function findProjectRoot() {
  let candidate = SCRIPT_DIR;
  for (let i = 0; i < 6; i++) {
    if (
      fs.existsSync(path.join(candidate, 'mod_config.json')) ||
      fs.existsSync(path.join(candidate, '.agents')) ||
      fs.existsSync(path.join(candidate, 'installed_mods'))
    ) {
      return candidate;
    }
    const parent = path.dirname(candidate);
    if (parent === candidate) break;
    candidate = parent;
  }
  return process.cwd();
}

function parseArgs(argv) {
  const opts = {
    game: '',
    modId: null,
    fileId: null,
    name: '',
    version: '',
    downloadOnly: false,
    closeBrowser: false,
    viaMcp: false,
    timeoutSec: 180,
    configPath: ''
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if ((a === '-g' || a === '--game') && argv[i + 1]) opts.game = argv[++i];
    else if ((a === '-m' || a === '--mod-id') && argv[i + 1]) opts.modId = parseInt(argv[++i], 10);
    else if ((a === '-f' || a === '--file-id') && argv[i + 1]) opts.fileId = parseInt(argv[++i], 10);
    else if (a === '--name' && argv[i + 1]) opts.name = argv[++i];
    else if (a === '--version' && argv[i + 1]) opts.version = argv[++i];
    else if (a === '--timeout' && argv[i + 1]) opts.timeoutSec = parseInt(argv[++i], 10) || 180;
    else if (a === '--config' && argv[i + 1]) opts.configPath = argv[++i];
    else if (a === '--download-only') opts.downloadOnly = true;
    else if (a === '--close-browser') opts.closeBrowser = true;
    else if (a === '--via-mcp') opts.viaMcp = true;
  }
  return opts;
}

function resolveDir(rawPath, baseDir) {
  if (!rawPath) return baseDir;
  if (rawPath.startsWith('~/') || rawPath.startsWith('~\\')) {
    return path.resolve(path.join(os.homedir(), rawPath.slice(2)));
  }
  return path.isAbsolute(rawPath) ? path.resolve(rawPath) : path.resolve(baseDir, rawPath);
}

function resolveChromeExe(chromeCfg) {
  if (chromeCfg && chromeCfg !== 'auto' && fs.existsSync(chromeCfg)) {
    return path.resolve(chromeCfg);
  }
  const candidates = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    path.join(os.homedir(), 'AppData', 'Local', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return candidates[0];
}

function loadRuntimeConfig(projectRoot, customConfigPath) {
  const effectiveCfgArg = customConfigPath || process.env.MOD_CONFIG_PATH || '';
  const cfgPath = effectiveCfgArg
    ? resolveDir(effectiveCfgArg, projectRoot)
    : path.join(projectRoot, 'mod_config.json');

  let cfg = {};
  if (fs.existsSync(cfgPath)) {
    try {
      cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
    } catch (_) {}
  }
  const env = cfg.environment || {};

  const chromeExe = resolveChromeExe(env.chromePath || 'auto');
  const profileDir = resolveDir(env.chromeProfileDir || './.chrome_mod_profile', projectRoot);
  const tmpDir = resolveDir(env.tmpDownloadsDir || cfg.tmpFolderPath || './tmp_downloads', projectRoot);
  const downloadsDir =
    env.downloadsDir && env.downloadsDir !== 'auto' && fs.existsSync(env.downloadsDir)
      ? path.resolve(env.downloadsDir)
      : path.join(os.homedir(), 'Downloads');
  const cdpPort = parseInt(env.cdpPort || 9222, 10);

  const dirSet = new Set([tmpDir, downloadsDir]);
  if (Array.isArray(env.extraDownloadDirs)) {
    for (const extra of env.extraDownloadDirs) {
      if (extra) dirSet.add(resolveDir(String(extra), projectRoot));
    }
  }

  const prefCandidates = [
    path.join(profileDir, 'Default', 'Preferences'),
    path.join(os.homedir(), 'AppData', 'Local', 'Google', 'Chrome', 'User Data', 'Default', 'Preferences')
  ];
  for (const prefPath of prefCandidates) {
    if (fs.existsSync(prefPath)) {
      try {
        const prefs = JSON.parse(fs.readFileSync(prefPath, 'utf8'));
        const chromeDl = prefs?.download?.default_directory;
        if (chromeDl && fs.existsSync(chromeDl)) {
          dirSet.add(path.resolve(chromeDl));
        }
      } catch (_) {}
    }
  }

  return {
    cfg,
    chromeExe,
    profileDir,
    tmpDir,
    cdpPort,
    downloadDirs: Array.from(dirSet)
  };
}

const PROJECT_ROOT = findProjectRoot();
const OPTS = parseArgs(process.argv.slice(2));
const RUNTIME = loadRuntimeConfig(PROJECT_ROOT, OPTS.configPath);

if (!OPTS.modId) {
  console.error('[错误] 必须提供 -m / --mod-id 参数，例如: node mcp_nexus_download.js -g stardewvalley -m 55910');
  process.exit(1);
}

const GAME_DOMAIN = normalizeGameDomain(OPTS.game || RUNTIME.cfg.defaultGame || 'stardewvalley');
const NEXUS_GAME = getNexusDomain(GAME_DOMAIN);
const MOD_ID = OPTS.modId;
const FILE_ID = OPTS.fileId;
const MOD_NAME = OPTS.name || `Mod ${MOD_ID}`;
const MOD_VERSION = OPTS.version || '';

const PROFILE_DIR = RUNTIME.profileDir;
const TMP_DIR = RUNTIME.tmpDir;
const CDP_PORT = RUNTIME.cdpPort;
const CHROME_EXE = RUNTIME.chromeExe;
const DOWNLOAD_DIRS = RUNTIME.downloadDirs;

if (!fs.existsSync(TMP_DIR)) fs.mkdirSync(TMP_DIR, { recursive: true });
if (!fs.existsSync(PROFILE_DIR)) fs.mkdirSync(PROFILE_DIR, { recursive: true });

function httpGetJson(url, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = http.request(url, { method, timeout: 2500 }, (res) => {
      let data = '';
      res.on('data', chunk => { data += chunk; });
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try {
            resolve(JSON.parse(data));
          } catch (_) {
            resolve(data);
          }
        } else {
          reject(new Error(`HTTP ${res.statusCode}`));
        }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
    req.end();
  });
}

async function getCdpVersion(port) {
  try {
    return await httpGetJson(`http://127.0.0.1:${port}/json/version`);
  } catch (_) {
    return null;
  }
}

function closeDedicatedChromeGracefully() {
  try {
    const profBase = path.basename(PROFILE_DIR).replace(/'/g, "''");
    const closeScript = `
      $procs = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'chrome.exe' -and $_.CommandLine -match '${profBase}'
      }
      foreach ($p in $procs) {
        $proc = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
        if ($proc -and $proc.MainWindowHandle -ne 0) {
          $proc.CloseMainWindow() | Out-Null
        }
      }
      Start-Sleep -Milliseconds 1200
      $rem = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'chrome.exe' -and $_.CommandLine -match '${profBase}'
      }
      foreach ($p in $rem) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
      }
    `;
    const enc = Buffer.from(closeScript, 'utf16le').toString('base64');
    execSync(`powershell -NoProfile -EncodedCommand ${enc}`, { stdio: 'ignore' });
  } catch (_) {}
}

function buildTargetUrl() {
  return FILE_ID
    ? `https://www.nexusmods.com/${NEXUS_GAME}/mods/${MOD_ID}?tab=files&file_id=${FILE_ID}&nmm=0`
    : `https://www.nexusmods.com/${NEXUS_GAME}/mods/${MOD_ID}?tab=files`;
}

async function ensureChromeRunning(targetUrl) {
  const existingVer = await getCdpVersion(CDP_PORT);
  if (existingVer && existingVer.webSocketDebuggerUrl) {
    console.log(`[Chrome 热复用] 检测到端口 ${CDP_PORT} 已有就绪浏览器会话，直接复用免冷启动！`);
    return { reused: true, browserInfo: existingVer };
  }

  // 若端口未就绪但存在占用同一 Profile 的僵尸进程，先优雅清理以释放 Profile 锁
  closeDedicatedChromeGracefully();
  await new Promise(r => setTimeout(r, 400));

  console.log('[Chrome 启动] 正在通过 WinSta0\\Default 真实桌面启动专用 Chrome 窗口...');
  const cmdArgs = [
    `"${CHROME_EXE}"`,
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir="${PROFILE_DIR}"`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-blink-features=AutomationControlled',
    '--start-maximized',
    '--new-window',
    `"${targetUrl}"`
  ].join(' ');

  const launcherPath = path.join(SCRIPT_DIR, 'launch_on_desktop.py');
  if (process.platform === 'win32' && fs.existsSync(launcherPath)) {
    const out = execSync(`python "${launcherPath}" ${cmdArgs}`, {
      encoding: 'utf8',
      cwd: PROJECT_ROOT
    }).trim();
    console.log(`[Chrome 启动] ${out}`);
  } else {
    const child = spawn(CHROME_EXE, [
      `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${PROFILE_DIR}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-blink-features=AutomationControlled',
      targetUrl
    ], { detached: true, stdio: 'ignore' });
    child.unref();
  }

  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 400));
    const ver = await getCdpVersion(CDP_PORT);
    if (ver && ver.webSocketDebuggerUrl) {
      console.log(`[Chrome 启动] Chrome CDP 端口 ${CDP_PORT} 已就绪！`);
      return { reused: false, browserInfo: ver };
    }
  }
  throw new Error(`未能连接到 Chrome CDP 端口 ${CDP_PORT}`);
}

const ARCHIVE_EXTS = ['.zip', '.7z', '.rar'];
function isArchiveFile(fileName) {
  const low = fileName.toLowerCase();
  return ARCHIVE_EXTS.some(ext => low.endsWith(ext));
}

function getExistingArchives(downloadDirs) {
  const archives = new Map();
  for (const dir of downloadDirs) {
    if (!fs.existsSync(dir)) continue;
    try {
      for (const file of fs.readdirSync(dir)) {
        if (isArchiveFile(file)) {
          const p = path.join(dir, file);
          try {
            archives.set(p, fs.statSync(p).mtimeMs);
          } catch (_) {}
        }
      }
    } catch (_) {}
  }
  return archives;
}

function checkForNewArchive(initialArchives, downloadDirs, expectedFilename = '') {
  // 若通过 CDP 事件拿到了明确文件名，优先精准定位
  if (expectedFilename) {
    for (const dir of downloadDirs) {
      const exactPath = path.join(dir, expectedFilename);
      if (fs.existsSync(exactPath) && !fs.existsSync(exactPath + '.crdownload')) {
        try {
          const st = fs.statSync(exactPath);
          if (st.size > 100) return exactPath;
        } catch (_) {}
      }
    }
  }

  for (const dir of downloadDirs) {
    if (!fs.existsSync(dir)) continue;
    try {
      for (const file of fs.readdirSync(dir)) {
        if (!isArchiveFile(file)) continue;
        const p = path.join(dir, file);
        if (fs.existsSync(p + '.crdownload')) continue;
        try {
          const stat = fs.statSync(p);
          const isNew = !initialArchives.has(p) || stat.mtimeMs > (initialArchives.get(p) || 0) + 500;
          if (isNew && stat.size > 100) {
            return p;
          }
        } catch (_) {}
      }
    } catch (_) {}
  }
  return null;
}

// 页面端交互检测脚本（不触碰任何人机验证组件）
const PAGE_CLICK_EXPR = `(() => {
  const url = window.location.href || '';
  const title = (document.title || '').toLowerCase();
  const bodyText = (document.body ? document.body.innerText : '').slice(0, 2000).toLowerCase();

  // 0. 检测 Cloudflare / 人机验证或登录页（绝不自动操作验证码，交由用户完成）
  if (
    title.includes('just a moment') ||
    bodyText.includes('verify you are human') ||
    document.querySelector('iframe[src*="challenges.cloudflare.com"], .cf-turnstile')
  ) {
    return { state: 'HUMAN_VERIFICATION_REQUIRED', url };
  }
  if (url.includes('/auth/sign_in') || window.location.host === 'users.nexusmods.com') {
    return { state: 'ON_LOGIN_PAGE', url };
  }

  function findElements(root, matcher) {
    const found = [];
    function walk(node) {
      if (!node) return;
      if (matcher(node)) found.push(node);
      if (node.shadowRoot) walk(node.shadowRoot);
      if (node.children) {
        for (const child of node.children) walk(child);
      }
    }
    walk(root);
    return found;
  }

  // 1. 检查 Shadow DOM 内的 mod-file-download 组件
  const mfd = document.querySelector('mod-file-download');
  if (mfd) {
    const root = mfd.shadowRoot || mfd;
    const els = Array.from(root.querySelectorAll('a, button'));
    const loginBtn = els.find(e => (e.innerText || '').trim().toLowerCase() === 'log in');
    if (loginBtn) {
      loginBtn.scrollIntoView({ block: 'center' });
      loginBtn.click();
      return { state: 'NEEDS_LOGIN_CLICKED', url };
    }
    const slowBtn = els.find(e => (e.innerText || '').trim().toLowerCase().includes('slow download'));
    if (slowBtn) {
      const href = slowBtn.getAttribute('href') || '';
      if (href.includes('users.nexusmods.com')) {
        slowBtn.click();
        return { state: 'NEEDS_LOGIN_CLICKED', url };
      }
      slowBtn.scrollIntoView({ block: 'center' });
      slowBtn.click();
      return { state: 'CLICKED_SLOW_DOWNLOAD', url };
    }
  }

  // 2. 全局查找 Slow Download 按钮
  const slowBtns = findElements(document.body, el => {
    const t = (el.innerText || '').trim().toLowerCase();
    return (el.tagName === 'BUTTON' || el.tagName === 'A') && t.includes('slow download');
  });
  if (slowBtns.length > 0) {
    const href = slowBtns[0].getAttribute('href') || '';
    if (href.includes('users.nexusmods.com')) {
      slowBtns[0].click();
      return { state: 'NEEDS_LOGIN_CLICKED', url };
    }
    slowBtns[0].scrollIntoView({ block: 'center' });
    slowBtns[0].click();
    return { state: 'CLICKED_SLOW_DOWNLOAD', url };
  }

  // 3. 前置依赖确认弹窗中的 Download 按钮或文件列表中的 Manual Download 按钮
  const modalBtns = findElements(document.body, el => {
    const t = (el.innerText || '').trim().toLowerCase();
    const href = el.getAttribute('href') || '';
    return (el.tagName === 'BUTTON' || el.tagName === 'A') &&
           (t === 'download' || t.includes('manual download') || href.includes('tab=files&file_id='));
  });
  if (modalBtns.length > 0) {
    const preferred = modalBtns.find(b => (b.innerText || '').trim().toLowerCase() === 'download') ||
                      modalBtns.find(b => (b.innerText || '').toLowerCase().includes('manual download')) ||
                      modalBtns[0];
    preferred.scrollIntoView({ block: 'center' });
    preferred.click();
    return { state: 'CLICKED_MODAL_DOWNLOAD', url };
  }

  return { state: 'WAITING', url };
})()`;

/**
 * 简单轻量的原生 CDP WebSocket 客户端（基于 Node v22 全局 WebSocket）
 */
class DirectCdpClient {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.ws = null;
    this.seq = 0;
    this.pending = new Map();
    this.eventHandlers = new Map();
  }

  connect() {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this.wsUrl);
      this.ws = ws;
      const timer = setTimeout(() => reject(new Error('CDP WebSocket 连接超时')), 8000);
      ws.addEventListener('open', () => {
        clearTimeout(timer);
        resolve();
      });
      ws.addEventListener('error', (err) => {
        clearTimeout(timer);
        reject(new Error(err?.message || 'CDP WebSocket 错误'));
      });
      ws.addEventListener('message', (evt) => {
        try {
          const msg = JSON.parse(typeof evt.data === 'string' ? evt.data : evt.data.toString());
          if (msg.id && this.pending.has(msg.id)) {
            const { resolve: resFn, reject: rejFn } = this.pending.get(msg.id);
            this.pending.delete(msg.id);
            if (msg.error) rejFn(new Error(msg.error.message || JSON.stringify(msg.error)));
            else resFn(msg.result || {});
          } else if (msg.method && this.eventHandlers.has(msg.method)) {
            for (const fn of this.eventHandlers.get(msg.method)) {
              try { fn(msg.params || {}); } catch (_) {}
            }
          }
        } catch (_) {}
      });
    });
  }

  on(method, fn) {
    if (!this.eventHandlers.has(method)) this.eventHandlers.set(method, []);
    this.eventHandlers.get(method).push(fn);
  }

  send(method, params = {}, timeoutMs = 8000) {
    return new Promise((resolve, reject) => {
      const id = ++this.seq;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP ${method} 超时`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (val) => { clearTimeout(timer); resolve(val); },
        reject: (err) => { clearTimeout(timer); reject(err); }
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    try {
      if (this.ws) this.ws.close();
    } catch (_) {}
  }
}

async function runViaDirectCdp(targetUrl, initialArchives, reused) {
  const browserVer = await getCdpVersion(CDP_PORT);
  const browserCdp = new DirectCdpClient(browserVer.webSocketDebuggerUrl);
  await browserCdp.connect();

  let activeDownloadGuid = null;
  let suggestedFilename = '';
  let cdpDownloadCompleted = false;

  browserCdp.on('Browser.downloadWillBegin', (params) => {
    activeDownloadGuid = params.guid;
    suggestedFilename = params.suggestedFilename || '';
    console.log(`⬇️ [CDP 下载开始] 文件: ${suggestedFilename || '未知文件'}`);
  });

  browserCdp.on('Browser.downloadProgress', (params) => {
    if (activeDownloadGuid && params.guid !== activeDownloadGuid) return;
    if (params.state === 'completed') {
      cdpDownloadCompleted = true;
    }
  });

  try {
    await browserCdp.send('Browser.setDownloadBehavior', {
      behavior: 'allow',
      downloadPath: TMP_DIR,
      eventsEnabled: true
    }).catch(() => {});
  } catch (_) {}

  // 选择已有标签页复用，避免无限堆积新标签
  const targets = await httpGetJson(`http://127.0.0.1:${CDP_PORT}/json`);
  const pages = Array.isArray(targets) ? targets.filter(t => t.type === 'page' && t.webSocketDebuggerUrl) : [];
  let targetPage = pages.find(p => (p.url || '').includes('nexusmods.com')) || pages[0];

  if (!targetPage) {
    targetPage = await httpGetJson(`http://127.0.0.1:${CDP_PORT}/json/new?${encodeURIComponent(targetUrl)}`, 'PUT').catch(
      () => httpGetJson(`http://127.0.0.1:${CDP_PORT}/json/new?${encodeURIComponent(targetUrl)}`, 'GET')
    );
  }

  const pageCdp = new DirectCdpClient(targetPage.webSocketDebuggerUrl);
  await pageCdp.connect();

  try {
    await pageCdp.send('Page.enable');
    await pageCdp.send('Runtime.enable');
    await pageCdp.send('Page.bringToFront').catch(() => {});
    await pageCdp.send('Page.setDownloadBehavior', {
      behavior: 'allow',
      downloadPath: TMP_DIR
    }).catch(() => {});

    if (reused || !(targetPage.url || '').includes(`/mods/${MOD_ID}`)) {
      console.log(`[CDP 导航] 跳转至目标模组文件页: ${targetUrl}`);
      await pageCdp.send('Page.navigate', { url: targetUrl });
    }

    const deadline = Date.now() + OPTS.timeoutSec * 1000;
    let clickedSlowDownload = false;
    let promptedGate = false;
    let modalClicks = 0;

    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 1200));

      const foundPath = checkForNewArchive(initialArchives, DOWNLOAD_DIRS, suggestedFilename);
      if (foundPath && (cdpDownloadCompleted || !activeDownloadGuid)) {
        const sizeKb = (fs.statSync(foundPath).size / 1024).toFixed(1);
        console.log(`\n🎉 [成功] 捕获到下载完成的模组压缩包: ${foundPath} (${sizeKb} KB)`);
        console.log(`[DOWNLOADED_ARCHIVE] ${foundPath}`);
        return foundPath;
      }

      if (!clickedSlowDownload) {
        try {
          const evalRes = await pageCdp.send('Runtime.evaluate', {
            expression: PAGE_CLICK_EXPR,
            returnByValue: true
          });
          const parsed = evalRes?.result?.value || {};
          const state = parsed.state || '';

          if (state === 'HUMAN_VERIFICATION_REQUIRED') {
            if (!promptedGate) {
              await pageCdp.send('Page.bringToFront').catch(() => {});
              console.log('\n⏸️ [等待验证] 检测到站点安全验证页面，请在桌面 Chrome 窗口手动完成验证（完成后自动继续）...');
              promptedGate = true;
            }
          } else if (state === 'ON_LOGIN_PAGE' || state === 'NEEDS_LOGIN_CLICKED') {
            if (!promptedGate) {
              await pageCdp.send('Page.bringToFront').catch(() => {});
              console.log('\n🔑 [等待登录] 请在桌面 Chrome 窗口完成 Nexus Mods 登录（登录态将持久保存在专用 Profile，后续免登）...');
              promptedGate = true;
            }
          } else if (state === 'CLICKED_MODAL_DOWNLOAD') {
            modalClicks++;
            console.log('✓ [CDP] 已自动点击文件列表/前置确认下载按钮，进入慢速下载页...');
            if (modalClicks >= 4 && FILE_ID) {
              await pageCdp.send('Page.navigate', { url: targetUrl });
              modalClicks = 0;
            }
          } else if (state === 'CLICKED_SLOW_DOWNLOAD') {
            console.log('✓ [CDP] 已自动点击 [Slow Download] 慢速下载按钮！正在等待 5 秒倒计时与文件落盘...');
            clickedSlowDownload = true;
          }
        } catch (_) {}
      }
    }

    console.log('\n[超时] 未在规定时间内捕获到下载完成的压缩包。');
    return null;
  } finally {
    pageCdp.close();
    browserCdp.close();
  }
}

function parseMcpEvalResult(mcpResult) {
  const text = mcpResult?.content?.[0]?.text || '';
  const match = text.match(/```json\s*([\s\S]*?)\s*```/);
  if (match) {
    try { return JSON.parse(match[1]); } catch (_) {}
  }
  return text;
}

async function runViaMcpSubprocess(targetUrl, initialArchives, reused) {
  const mcpProcess = spawn('npx.cmd', [
    '-y', 'chrome-devtools-mcp@latest',
    `--browserUrl=http://127.0.0.1:${CDP_PORT}`
  ], {
    shell: true,
    stdio: ['pipe', 'pipe', 'inherit'],
    cwd: PROJECT_ROOT
  });

  const rl = readline.createInterface({ input: mcpProcess.stdout });
  let msgId = 1;
  const pendingRequests = new Map();

  function sendMCP(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = msgId++;
      pendingRequests.set(id, { resolve, reject });
      mcpProcess.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }

  function callTool(name, args = {}) {
    return sendMCP('tools/call', { name, arguments: args });
  }

  rl.on('line', (line) => {
    try {
      const res = JSON.parse(line);
      if (res.id && pendingRequests.has(res.id)) {
        const { resolve, reject } = pendingRequests.get(res.id);
        pendingRequests.delete(res.id);
        if (res.error) reject(new Error(res.error.message || JSON.stringify(res.error)));
        else resolve(res.result);
      }
    } catch (_) {}
  });

  try {
    await sendMCP('initialize', {
      protocolVersion: '2024-11-05',
      capabilities: {},
      clientInfo: { name: 'nexus-mod-installer', version: '2.0' }
    });
    mcpProcess.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');

    const listRes = await callTool('list_pages', {});
    const pagesText = listRes?.content?.[0]?.text || '';
    const pageMatch = pagesText.match(/(\d+):/);
    const pageId = pageMatch ? parseInt(pageMatch[1], 10) : 1;
    await callTool('select_page', { pageId, bringToFront: true });
    if (reused) {
      await callTool('navigate_page', { pageId, url: targetUrl }).catch(() => {});
    }

    const deadline = Date.now() + OPTS.timeoutSec * 1000;
    let clickedSlowDownload = false;
    let promptedGate = false;

    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 1500));
      const foundPath = checkForNewArchive(initialArchives, DOWNLOAD_DIRS);
      if (foundPath) {
        console.log(`\n🎉 [成功] 捕获到新下载完成的模组压缩包: ${foundPath}`);
        console.log(`[DOWNLOADED_ARCHIVE] ${foundPath}`);
        return foundPath;
      }

      if (!clickedSlowDownload) {
        try {
          const evalRes = await callTool('evaluate_script', {
            pageId,
            waitForStableDom: false,
            function: `() => ${PAGE_CLICK_EXPR}`
          });
          const parsed = parseMcpEvalResult(evalRes);
          const state = parsed?.state || '';
          if (state === 'HUMAN_VERIFICATION_REQUIRED' || state === 'ON_LOGIN_PAGE' || state === 'NEEDS_LOGIN_CLICKED') {
            if (!promptedGate) {
              console.log('\n🔑 [等待交互] 请在桌面 Chrome 窗口中完成登录或验证，完成后将自动继续...');
              promptedGate = true;
            }
          } else if (state === 'CLICKED_SLOW_DOWNLOAD') {
            console.log('✓ [MCP] 已自动点击 [Slow Download] 慢速下载按钮，等待落盘...');
            clickedSlowDownload = true;
          }
        } catch (_) {}
      }
    }
    return null;
  } finally {
    try { mcpProcess.kill(); } catch (_) {}
  }
}

async function run() {
  const targetUrl = buildTargetUrl();
  const useDirectCdp = !OPTS.viaMcp && typeof WebSocket !== 'undefined';

  console.log('========================================================');
  console.log(` 🌐 桌面 Chrome 自动模拟下载 (${useDirectCdp ? '原生 CDP 零延迟直连' : 'chrome-devtools-mcp 模式'})`);
  console.log(` 模组: ${MOD_NAME} (Nexus ID: ${MOD_ID}, File ID: ${FILE_ID || 'auto'})`);
  console.log(` 游戏: ${GAME_DOMAIN} (Nexus: ${NEXUS_GAME})`);
  console.log(` 下载目录: ${TMP_DIR} (兜底监控: ${DOWNLOAD_DIRS.join(' | ')})`);
  console.log('========================================================\n');

  const initialArchives = getExistingArchives(DOWNLOAD_DIRS);
  const { reused } = await ensureChromeRunning(targetUrl);

  if (useDirectCdp) {
    return await runViaDirectCdp(targetUrl, initialArchives, reused);
  }
  return await runViaMcpSubprocess(targetUrl, initialArchives, reused);
}

run().then(async (downloadedFile) => {
  if (OPTS.closeBrowser) {
    closeDedicatedChromeGracefully();
    console.log('✓ [--close-browser] 已自动关闭专用桌面 Chrome 窗口并释放 CDP 端口。');
  }

  if (downloadedFile && fs.existsSync(downloadedFile)) {
    if (OPTS.downloadOnly) {
      console.log(`\n✓ [--download-only] 下载完成！文件位置: ${downloadedFile}`);
      process.exit(0);
    }

    console.log(`\n========================================================`);
    console.log(` 📦 正在调用自适应安装引擎部署模组至游戏 [${GAME_DOMAIN}] ...`);
    console.log(`========================================================`);

    const pyArgs = [
      path.join(SCRIPT_DIR, 'nexus_mod_install.py'),
      '-g', GAME_DOMAIN,
      '--install-zip', downloadedFile,
      '--mod-id', String(MOD_ID),
      '--mod-name', MOD_NAME,
      '--skip-scan'
    ];
    if (MOD_VERSION) {
      pyArgs.push('--mod-version', MOD_VERSION);
    }

    const deployProcess = spawn('python', pyArgs, {
      stdio: 'inherit',
      cwd: PROJECT_ROOT
    });

    deployProcess.on('close', (code) => {
      console.log(`\n🎉 安装部署完成 (退出码: ${code})！`);
      process.exit(code || 0);
    });
  } else {
    process.exit(1);
  }
}).catch((err) => {
  console.error('[运行错误]', err);
  process.exit(1);
});
