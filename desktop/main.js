/**
 * StaffDeck Windows 桌面壳 —— Electron 主进程
 *
 * 职责：把 StaffDeck（Python 后端 + 已挂载的前端）装进一个独立原生窗口，
 * 替代默认的"启动后在系统浏览器中打开"体验。
 *
 * 复用的现有能力：
 *   - 后端服务：backend/desktop_launcher.py（端口探测、健康检查、env 注入）
 *   - 健康检查接口：GET /api/health  → 返回 {"status":"ok","app":"StaffDeck"}
 *   - 界面入口：http://127.0.0.1:<port>/chat/
 *
 * 运行模式（按优先级）：
 *   1) 环境变量 STAFFDECK_URL —— 直接连接该地址（适合 dev 脚本已拉起后端）
 *   2) 探测本地 5173–5199 端口是否有健康的 StaffDeck —— 命中则直接连
 *   3) 环境变量 STAFFDECK_BACKEND —— 自动拉起指定的后端可执行文件
 *   4) 默认假定 PATH 里有 staffdeck 可执行文件，自动拉起
 */
const { app, BrowserWindow, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');

const APP_NAME = 'StaffDeck';
const HEALTH_PATH = '/api/health';
const CHAT_PATH = '/chat/';

// 与 backend/desktop_launcher.py 的默认端口范围保持一致
const PORT_RANGE = { start: 5173, end: 5199 };

let serverProc = null;

/**
 * 对指定 URL 做一次健康检查，返回 true 表示 StaffDeck 已就绪。
 */
function checkHealth(baseUrl) {
  return new Promise((resolve) => {
    const req = http.get(`${baseUrl}${HEALTH_PATH}`, { timeout: 1200 }, (res) => {
      let body = '';
      res.on('data', (chunk) => (body += chunk));
      res.on('end', () => {
        try {
          const payload = JSON.parse(body);
          resolve(payload.status === 'ok' && payload.app === APP_NAME);
        } catch {
          resolve(false);
        }
      });
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

/**
 * 轮询等待后端就绪，默认最多 60 秒。
 */
async function waitForHealth(baseUrl, attempts = 120, delay = 500) {
  for (let i = 0; i < attempts; i += 1) {
    if (await checkHealth(baseUrl)) return true;
    await new Promise((resolve) => setTimeout(resolve, delay));
  }
  return false;
}

/**
 * 在本地端口范围内探测一个已经健康运行的 StaffDeck 实例。
 */
async function probeRunningInstance(host = '127.0.0.1') {
  for (let port = PORT_RANGE.start; port <= PORT_RANGE.end; port += 1) {
    const url = `http://${host}:${port}`;
    if (await checkHealth(url)) return url;
  }
  return null;
}

/**
 * 定位后端可执行入口（开发环境 / 打包环境）。
 *
 * 返回 { cmd, args }：
 *   - cmd  可执行文件本身（python.exe / staffdeck.exe / 自定义）
 *   - args 要追加的命令行参数
 * 解析顺序（配合 main.js 的自动拉起）：
 *   - STAFFDECK_BACKEND 显式指定时优先（可配合 STAFFDECK_BACKEND_SCRIPT 传脚本）
 *   - 否则若有 backend/.venv，则用其中的 python 解释器跑 desktop_launcher.py
 *     （开发态；判定与 npm 脚本名无关，见函数内注释）
 *   - 否则回退到 PATH 中的 staffdeck（打包态）
 */
function resolveBackendEntry() {
  const backend = process.env.STAFFDECK_BACKEND;
  if (backend) {
    const script = process.env.STAFFDECK_BACKEND_SCRIPT;
    if (script && path.isAbsolute(script)) {
      return { cmd: backend, args: [script] };
    }
    return { cmd: backend, args: [] };
  }

  // 开发态：优先用仓库里的 backend/.venv。不依赖 npm 脚本名或 STAFFDECK_DEV，
  // 只要 .venv 存在就用它；打包态 .venv 不在安装包内，自然回退到 staffdeck。
  const fs = require('fs');
  const venvPy = path.join(__dirname, '..', 'backend', '.venv', 'Scripts', 'python.exe');
  const launcher = path.join(__dirname, '..', 'backend', 'desktop_launcher.py');
  if (fs.existsSync(venvPy) && fs.existsSync(launcher)) {
    return { cmd: venvPy, args: [launcher] };
  }

  // 打包态：PATH 中的 staffdeck
  return { cmd: 'staffdeck', args: [] };
}

/**
 * 自动拉起后端进程，并在就绪后回调实际端口解析出的 URL。
 */
async function spawnBackend() {
  const { cmd, args } = resolveBackendEntry();
  // 参数顺序：脚本在前、服务参数在后（python desktop_launcher.py --host ...）
  const launchArgs = [...args, '--host', '127.0.0.1'];
  serverProc = spawn(cmd, launchArgs, {
    stdio: 'inherit',
    windowsHide: true,
  });
  serverProc.on('error', (err) => {
    dialog.showErrorBox(
      `${APP_NAME} 启动失败`,
      `无法启动后端进程：${cmd}\n\n${err.message}\n\n` +
        '请确认后端环境已就绪（backend/.venv），或设置 STAFFDECK_BACKEND 指定后端路径。'
    );
  });
  serverProc.on('close', () => {
    // 后端退出被视为应用退出，避免后台残留进程
    app.quit();
  });
  return serverProc;
}

/**
 * 解析最终要加载的后端地址。
 * 自动拉起后端后，会探测端口范围找到它实际监听的实例。
 * 返回 { baseUrl, spawned }。
 */
async function resolveBackendUrl() {
  // 1) 显式指定 URL
  const explicit = process.env.STAFFDECK_URL;
  if (explicit) {
    const url = explicit.replace(/\/+$/, '');
    if (await checkHealth(url)) {
      return { baseUrl: url, spawned: false };
    }
    // 显式 URL 未就绪：等待它就绪（假设由 dev 脚本拉起）
    if (await waitForHealth(url)) {
      return { baseUrl: url, spawned: false };
    }
  }

  // 2) 探测是否已有运行中的实例（并发跑 dev 脚本、或重复打开壳）
  const running = await probeRunningInstance();
  if (running) {
    return { baseUrl: running, spawned: false };
  }

  // 3) 自动拉起后端，然后探测其实际端口
  await spawnBackend();
  for (let i = 0; i < 120; i += 1) {
    const found = await probeRunningInstance();
    if (found) {
      return { baseUrl: found, spawned: true };
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }

  throw new Error(`后端在 ${PORT_RANGE.start}–${PORT_RANGE.end} 端口上 60 秒内未就绪。`);
}

function createWindow(baseUrl) {
  const win = new BrowserWindow({
    title: APP_NAME,
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    autoHideMenuBar: true,
    backgroundColor: '#0a0a0a',
    icon: process.env.STAFFDECK_ICON
      ? path.resolve(process.env.STAFFDECK_ICON)
      : undefined,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  win.loadURL(`${baseUrl}${CHAT_PATH}`);

  // 非本机链接交给系统浏览器，保持壳的封闭性
  win.webContents.setWindowOpenHandler(({ url }) => {
    const internal =
      url.startsWith(baseUrl) ||
      url.startsWith(`http://localhost:`) ||
      /^http:\/\/127\.0\.0\.1:\d+/.test(url);
    if (internal) {
      return { action: 'allow' };
    }
    require('electron').shell.openExternal(url);
    return { action: 'deny' };
  });

  win.on('closed', () => {
    if (serverProc) {
      serverProc.kill();
      serverProc = null;
    }
  });

  return win;
}

app.whenReady().then(async () => {
  try {
    const { baseUrl, spawned } = await resolveBackendUrl();
    console.log(`${APP_NAME} 后端就绪：${baseUrl} (spawned=${spawned})`);
    createWindow(baseUrl);
  } catch (err) {
    dialog.showErrorBox(`${APP_NAME} 启动失败`, err.message);
    app.quit();
  }
});

app.on('window-all-closed', () => {
  app.quit();
});
