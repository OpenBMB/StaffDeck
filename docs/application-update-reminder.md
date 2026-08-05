# Application update reminder

StaffDeck desktop builds perform a best-effort check for newer GitHub releases after a user
signs in and completes the first-run guides. The feature only displays a reminder; it never
downloads or installs an update.

## Runtime behavior

`GET /api/app/version` returns the current application version and the newest compatible release
from `https://github.com/OpenBMB/StaffDeck/releases.atom`. A stable build ignores prereleases. A
prerelease build may advance to a newer prerelease or a stable release.

Successful checks are cached for six hours and failed checks for fifteen minutes. Network,
parsing, and invalid-version failures are reported as `check_succeeded=false` and do not affect
normal application use.

The frontend waits until both first-run guides are complete. It then shows a 30-second toast only
when an update exists. The key below records the announced release in that browser, so refreshing
does not repeat the same version:

```text
staffdeck_update_reminded_version
```

The release URL is accepted only when it is an HTTPS GitHub URL under the StaffDeck release-tag
path. Packaged macOS builds hand external links from the embedded StaffDeck window to the system
browser. Other platforms already display StaffDeck in the system browser.

Update comparison compatibility is guaranteed from the `0.2.0` stable release onward. Historical
`0.12-beta.*` development builds predate that version line and are not supported as update-check
starting points. Stable builds ignore all prerelease tags, so those historical tags cannot affect
updates between supported stable versions such as `0.2.0` and `0.2.1`.

## Enablement

Packaged PyInstaller applications enable update checks by default. Source and private deployments
disable them by default to avoid unexpected outbound traffic. They can opt in or out explicitly:

```text
STAFFDECK_UPDATE_CHECK=true
STAFFDECK_UPDATE_CHECK=false
```

`STAFFDECK_VERSION` overrides the current version for development and deployment testing.

## Build version

`packaging/ultrarag.spec` reads `VERSION`, validates it, and writes
`packaging/build/staffdeck-version.txt`. PyInstaller bundles that file with the application.
Runtime lookup supports the PyInstaller extraction directory, an executable-adjacent resource,
and macOS `Contents/Resources`.

Example:

```bash
VERSION=v0.2.0 bash packaging/build_macos.sh
```

## Manual acceptance

1. Start a packaged build, or source deployment with checks enabled, using a version older than
   the latest GitHub release.
2. Sign in and complete or close both first-run guides.
3. Confirm the toast identifies the current and latest versions and links to the matching trusted
   GitHub release.
4. Close the toast and refresh. Confirm the same release is not shown again.
5. Test while offline and confirm the rest of StaffDeck remains usable.
6. Test a stable current version against a feed containing only prereleases and confirm no update
   is offered.
