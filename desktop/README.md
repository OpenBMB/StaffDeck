# StaffDeck 桌面壳（Electron）

把 StaffDeck（Python 后端 + 已挂载的前端）包装成一个独立的 Windows 桌面应用，
替代默认的"启动后在系统浏览器中打开"体验。

> **复用现有能力，不重写任何后端/前端代码**：壳只是负责"拉起后端 + 起一个
> 独立窗口加载 `http://127.0.0.1:<port>/chat/`"。

## 目录结构
```
desktop/
├── main.js               # Electron 主进程：起窗口、拉起后端、健康检查、清理进程
├── preload.js            # 渲染进程安全桥（最小桥接面）
├── package.json          # 依赖与脚本
├── electron-builder.yml  # 打包成 Windows 安装包的配置
└── resources/            # 打包资源（可选，git 忽略）
```

## 开发调试（最快跑通）

先按官方方式把后端跑起来（复用现有脚本），再起 Electron 连上去：

```powershell
# 1) 启动后端（来自仓库根目录的现有脚本）
.\scripts\dev_up.ps1 up

# 2) 安装壳依赖
cd desktop
npm install

# 3) 告诉壳后端地址，然后启动
$env:STAFFDECK_URL = "http://127.0.0.1:5173"
npm start
```

壳启动时会按如下顺序解析后端地址：

| 优先级 | 方式 | 说明 |
|---|---|---|
| 1 | `STAFFDECK_URL` 环境变量 | 直接连接该地址，等待就绪 |
| 2 | 探测本地 `5173–5199` 端口 | 命中健康的 StaffDeck 实例则直接连 |
| 3 | `STAFFDECK_BACKEND` 环境变量 | 自动拉起指定的后端可执行文件 |
| 4 | PATH 中的 `staffdeck` | 自动拉起 |

## 生产打包（生成 Windows 安装包）

```powershell
cd desktop
npm install
npm run dist          # 产物：desktop/dist/StaffDeck-Setup-<version>.exe
```

electron-builder 会通过 `extraResources` 把 `packaging/out/staffdeck`（打包好的
Python 后端）一并打进安装包；`main.js` 在首次启动时会自动拉起它。

若后端尚未打包，可先用官方流程生成：
```powershell
$env:VERSION = "0.4.0"
.\packaging\build_windows.ps1   # 产出 packaging/out/staffdeck/staffdeck.exe
```

## 常用环境变量

| 变量 | 作用 |
|---|---|
| `STAFFDECK_URL` | 指定要连接的后端地址（dev 场景最常用） |
| `STAFFDECK_BACKEND` | 指定要自动拉起的后端可执行文件路径 |
| `STAFFDECK_ICON` | 覆盖窗口图标路径 |
| `STAFFDECK_HEADLESS` | 后端侧环境变量，壳一般不需要 |
