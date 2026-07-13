; packaging/installer/ultrarag.iss — Inno Setup 脚本（产物为 URStaff）
; 由 build_windows.ps1 调用：ISCC.exe packaging\installer\ultrarag.iss
; VERSION 通过环境变量传入（GetEnv）

[Setup]
AppId=URStaff
AppName=URStaff
AppVersion={#GetEnv('VERSION')}
AppVerName=URStaff {#GetEnv('VERSION')}
AppPublisher=URStaff
DefaultDirName={autopf}\URStaff
DefaultGroupName=URStaff
OutputDir=..\out
OutputBaseFilename=URStaff-setup
SetupIconFile=..\assets\staffdeck.ico
UninstallDisplayIcon={app}\staffdeck.exe
UninstallDisplayName=URStaff
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
WizardStyle=modern
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=no
DisableReadyPage=no
VersionInfoVersion={#GetEnv('VERSION')}
VersionInfoProductName=URStaff
VersionInfoProductVersion={#GetEnv('VERSION')}
VersionInfoCompany=URStaff
VersionInfoDescription=URStaff Installer
#if GetEnv('WINDOWS_SIGN_ENABLED') == '1'
SignTool=urstaff
SignedUninstaller=yes
#endif

[Files]
; PyInstaller onedir 产物整体安装
Source: "..\out\staffdeck\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\URStaff"; Filename: "{app}\staffdeck.exe"; AppUserModelID: "ai.urstaff.desktop"
Name: "{autodesktop}\URStaff"; Filename: "{app}\staffdeck.exe"; AppUserModelID: "ai.urstaff.desktop"

[Run]
Filename: "{app}\staffdeck.exe"; Description: "启动 URStaff"; Flags: postinstall nowait skipifsilent
