# packaging/ultrarag.spec
# 运行：cd backend && pyinstaller ../packaging/ultrarag.spec --noconfirm
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

BACKEND = Path.cwd()                      # 约定在 backend/ 下执行
REPO = BACKEND.parent
DIST = REPO / "frontend-enterprise" / "dist"
ASSETS = REPO / "packaging" / "assets"
ICNS = ASSETS / "staffdeck.icns"
ICO = ASSETS / "staffdeck.ico"
assert DIST.exists(), "先构建前端：npm --prefix frontend-enterprise run build"

# 平台图标：macOS 用 .icns，Windows 用 .ico，Linux(EXE) 不用
_exe_icon = None
if sys.platform == "win32" and ICO.exists():
    _exe_icon = str(ICO)

datas = [
    (str(DIST), "frontend-enterprise/dist"),
    (str(BACKEND / "app" / "llm" / "prompts"), "app/llm/prompts"),
    (str(BACKEND / "app" / "db" / "seed_fixtures"), "app/db/seed_fixtures"),
    (str(BACKEND / "mock_servers"), "mock_servers"),
]

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("sqlmodel")
    + collect_submodules("app")
    + [
        # 顶层单文件模块：uvicorn 用字符串 "single_port_app:app" 运行时动态 import
        "single_port_app",
        "cryptography", "certifi", "python_multipart", "docx", "pypdf", "bs4", "openai",
        # 动态导入补充：pydantic/starlette/anyio 等
        "pydantic", "pydantic_settings", "pydantic.deprecated.decorator",
        "starlette", "anyio", "email_validator", "sqlalchemy",
    ]
)

# macOS：Dock 壳需要 pyobjc（AppKit + PyObjCTools）
if sys.platform == "darwin":
    hiddenimports = hiddenimports + collect_submodules("objc") + [
        "AppKit", "Foundation", "PyObjCTools", "PyObjCTools.AppHelper",
    ]

a = Analysis(
    [str(BACKEND / "desktop_launcher.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
# console=False：作为 GUI app 常驻 Dock（console=True 会加 LSBackgroundOnly 变纯后台不进 Dock）。
# 日志由 launcher 重定向到用户数据目录，启动问题可查文件。
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="staffdeck",
          console=False, disable_windowed_traceback=False, icon=_exe_icon)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="staffdeck")

# macOS：额外产出标准 .app bundle（PyInstaller 正确处理 Contents/Frameworks 布局）。
# 附带 python runtime 由 build 脚本在打包后拷进 .app/Contents/MacOS/runtime。
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="URStaff.app",
        icon=str(ICNS) if ICNS.exists() else None,
        bundle_identifier="ai.urstaff.desktop",
        info_plist={
            "CFBundleName": "URStaff",
            "CFBundleDisplayName": "URStaff",
            # 可执行名保持 staffdeck（COLLECT/EXE 名 + build 脚本按此路径拷 runtime）
            "CFBundleExecutable": "staffdeck",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "NSHighResolutionCapable": True,
            # 显式声明为常规 GUI app：进 Dock、可激活（非后台/非 agent）
            "LSBackgroundOnly": False,
            "LSUIElement": False,
        },
    )
