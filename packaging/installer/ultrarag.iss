; packaging/installer/ultrarag.iss — Inno Setup 脚本（产物为 StaffDeck）
; 由 build_windows.ps1 调用：ISCC.exe packaging\installer\ultrarag.iss
; VERSION 通过环境变量传入（GetEnv）

[Setup]
AppId=StaffDeck
AppName=StaffDeck
AppVersion={#GetEnv('VERSION')}
AppVerName=StaffDeck {#GetEnv('VERSION')}
AppPublisher=StaffDeck
DefaultDirName={autopf}\StaffDeck
DefaultGroupName=StaffDeck
OutputDir=..\out
OutputBaseFilename=StaffDeck-setup
SetupIconFile=..\assets\staffdeck.ico
UninstallDisplayIcon={app}\staffdeck.exe
UninstallDisplayName=StaffDeck
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64os
PrivilegesRequired=lowest
WizardStyle=modern
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=no
DisableReadyPage=no
VersionInfoVersion={#GetEnv('WINDOWS_VERSION_INFO_VERSION')}
VersionInfoProductName=StaffDeck
VersionInfoProductVersion={#GetEnv('WINDOWS_VERSION_INFO_VERSION')}
VersionInfoCompany=StaffDeck
VersionInfoDescription=StaffDeck Installer
#if GetEnv('WINDOWS_SIGN_ENABLED') == '1'
SignTool=staffdeck
SignedUninstaller=yes
#endif

[Files]
; PyInstaller onedir 产物整体安装
Source: "..\out\staffdeck\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Dirs]
; The dedicated SRT account must be able to launch bundled runtimes.
Name: "{app}"; Permissions: users-readexec

[Registry]
Root: HKCU; Subkey: "Software\Classes\staffdeck"; ValueType: string; ValueData: "URL:StaffDeck Protocol"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\staffdeck"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\staffdeck\DefaultIcon"; ValueType: string; ValueData: "{app}\staffdeck.exe,0"
Root: HKCU; Subkey: "Software\Classes\staffdeck\shell\open\command"; ValueType: string; ValueData: """{app}\staffdeck.exe"" ""%1"""

[Icons]
Name: "{group}\StaffDeck"; Filename: "{app}\staffdeck.exe"; AppUserModelID: "ai.staffdeck.desktop"
Name: "{autodesktop}\StaffDeck"; Filename: "{app}\staffdeck.exe"; AppUserModelID: "ai.staffdeck.desktop"

[Run]
Filename: "{app}\staffdeck.exe"; Description: "启动 StaffDeck"; Flags: postinstall nowait skipifsilent
