# Upgrade Guide

This guide explains how to upgrade an existing StaffDeck deployment to a newer
`main` checkout or release tag. Database schema changes are applied
automatically on startup (`SQLModel.metadata.create_all` plus the incremental
SQLite migrations in `backend/app/db/database.py`), so no manual migration step
is required for either deployment mode. The supported production migration path
currently assumes SQLite.

[INSTALL.md](../INSTALL.md) covers the same upgrades in Chinese, including the
runtime preparation `scripts/dev_up.sh` performs and the pre-upgrade database
backup written to `backups/`; this page is the English summary linked from the
README.

## Before you upgrade

Back up the whole data directory, not only the database: attachments and
workspaces live there and are not stored in SQLite.

| Deployment | What to back up |
| --- | --- |
| Desktop / packaged app (macOS) | `~/Library/Application Support/StaffDeck/` |
| Desktop / packaged app (Windows) | `%APPDATA%\StaffDeck\` |
| Desktop / packaged app (Linux) | `~/.local/share/StaffDeck/` |
| Source deployment | that data directory, plus `backend/skill_agent_loop.db` (or the database in `DATABASE_URL`) and `backend/.env` |

If `ULTRARAG_DATA_DIR` is set, the data directory is that path instead. If you
customized `DATABASE_URL` in `backend/.env`, back up that database rather than
the default file.

The data directory holds `harness_uploads/` (attachments) and
`harness_workspaces/` (workspace files) next to `backups/`, so a copy of the
SQLite file alone cannot restore them. Copy the files while the application is
stopped; before applying schema changes the application also writes its own
permission-restricted copy to `backups/pre-upgrade-*.db`.

## Upgrading a desktop / packaged installation

1. Quit StaffDeck.
2. Download the latest installer for your platform from the
   [releases page](https://github.com/OpenBMB/StaffDeck/releases/latest) and
   install it over the existing installation (`.dmg` on macOS, setup `.exe` on
   Windows, `.deb` on Linux).
3. Launch StaffDeck. Your data directory is untouched by the installer, and
   any schema changes are applied automatically on first launch.

Desktop builds can also remind you when a newer release is available; see
[application-update-reminder.md](application-update-reminder.md) for how the
check works and how to enable it for source or private deployments.

## Upgrading a source deployment

How you restart depends on how the application was started.

### Services started with `scripts/dev_up.sh`

From the repository root:

```bash
# 1. Update the code
git fetch --tags origin
git checkout main
git pull origin main

# 2. Restart; preparation runs before the old process is stopped
scripts/dev_up.sh --detach
```

`dev_up.sh` builds missing Harness runtime and frontend artifacts and installs
the local Python SDK before it stops the process it started, so the old service
keeps running if preparation fails. `scripts/dev_down.sh` stops only the
processes `dev_up.sh` started.

### Services started with `staffdeck serve`, `launch.py` or a bare uvicorn

`scripts/dev_down.sh` does not know about these processes, so stop them
yourself first (Ctrl+C or your service manager):

```bash
# 1. Stop the running server, then update the code
git fetch --tags origin
git checkout main
git pull origin main

# 2. Prepare the runtime, then start the same command as before
python3 prepare.py
staffdeck serve
```

`prepare.py` rebuilds the frontend, Harness runtime and local Python SDK after
the update; without it the previous build artifacts stay in place. Do not
start the new version while the old one is still running: both would write to
the same database.

On Windows, use `.\scripts\dev_up.ps1 --detach` for the dev-script path and
`python prepare.py` for a `staffdeck serve` or `launch.py` service.

### Choosing the target

`main` tracks development. To pin a release instead, check out its tag and
restart the same way:

```bash
git fetch --tags origin
git checkout v1.0.0-preview
```

`backend/.env` is ignored by git, so your model endpoints, API keys, and
`APP_SECRET` are preserved across upgrades. Do not regenerate `APP_SECRET`
during an upgrade — existing encrypted credentials depend on it.

## Verify the upgrade

1. Open the app on the port it prints at startup (default
   `http://127.0.0.1:5173/`) and confirm `GET /api/health` responds, for example
   `curl http://127.0.0.1:5173/api/health`.
2. Sign in and confirm your digital employees, sessions, knowledge bases and
   scheduled tasks are present.
3. If anything looks wrong, stop the app, restore the backed-up data directory
   and database file, and reinstall the previous release (or check out the
   previous tag).
