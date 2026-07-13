# Windows code signing

URStaff uses Authenticode signing for the packaged application, the Inno Setup
uninstaller, and the final installer. Signing is optional for local builds but
should be enabled for public releases.

## Prerequisites

1. Install the Windows SDK so `signtool.exe` is available, or set
   `SIGNTOOL_EXE` to its full path.
2. Obtain an organization-validated Windows code-signing certificate. Prefer a
   certificate installed in the Windows certificate store or a hardware-backed
   key for release workstations.

## Certificate store

```powershell
$env:WINDOWS_CERT_THUMBPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
$env:VERSION = "0.1.0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

## PFX file

```powershell
$env:WINDOWS_PFX_PATH = "C:\secure\urstaff-code-signing.pfx"
$env:WINDOWS_PFX_PASSWORD = "<secret>"
$env:VERSION = "0.1.0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

Do not commit a PFX file or its password. In CI, store both as protected
secrets. `WINDOWS_TIMESTAMP_URL` defaults to DigiCert's RFC 3161 timestamp
service and can be overridden when required.

The build fails if signing or verification fails. Without either certificate
variable, the build continues for local testing and prints an explicit
`UNSIGNED` warning.
