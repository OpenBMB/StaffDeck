# Windows code signing

StaffDeck uses Authenticode signing for the packaged application, the bundled
Node/SRT executables, the Inno Setup uninstaller, and the final installer.
Signing is required for any package distributed to another Windows machine.

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
$env:WINDOWS_PFX_PATH = "C:\secure\staffdeck-code-signing.pfx"
$env:WINDOWS_PFX_PASSWORD = "<secret>"
$env:VERSION = "0.1.0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

## Cloud or CI signing

When the signing key is held by a cloud service or an HSM, set
`WINDOWS_SIGNER_SCRIPT` to a PowerShell script supplied by the CI job:

```powershell
$env:WINDOWS_SIGNER_SCRIPT = "$env:RUNNER_TEMP\sign-with-cloud.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

The external script must accept a mandatory `-FilePath` argument, sign that
file in place, wait for the remote signing operation to finish, and exit
non-zero on failure. The repository wrapper independently calls
`Get-AuthenticodeSignature` after every invocation, so a successful command
that did not produce a valid signature still fails the build. This contract
supports Azure Trusted Signing and other remote signing providers without
placing a PFX or private key in the repository or runner.

The signing identity must be trusted by the Windows application-control policy
used by the target machines. A self-signed certificate only works on a managed
fleet where its trust chain and matching WDAC/AppLocker policy are deployed by
administrators; it is not a public-distribution substitute.

Do not commit a PFX file or its password. In CI, store both as protected
secrets. `WINDOWS_TIMESTAMP_URL` defaults to DigiCert's RFC 3161 timestamp
service and can be overridden when required.

The build fails closed when no certificate or external signer is configured.
For a local-only developer build that will not be distributed, explicitly set
`$env:WINDOWS_ALLOW_UNSIGNED = "1"`; the output will remain marked as
`UNSIGNED` and must not be sent to customers.
