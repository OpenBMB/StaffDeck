# Upgrade Guide

This guide explains how to upgrade an existing StaffDeck deployment to the
latest release. Database schema changes are applied automatically on startup
(`SQLModel.metadata.create_all` plus the incremental SQLite migrations in
`backend/app/db/database.py`), so no manual migration step is required for
either deployment mode. The supported production migration path currently
assumes SQLite.

## Before you upgrade

Back up your data first. Where it lives depends on how you run StaffDeck:

| Deployment | Data location |
| --- | --- |
| Desktop / packaged app (macOS) | `~/Library/Application Support/StaffDeck/` |
| Desktop / packaged app (Windows) | `%APPDATA%\StaffDeck\` |
| Desktop / packaged app (Linux) | `~/.local/share/StaffDeck/` |
| Source deployment | `backend/skill_agent_loop.db` (default `DATABASE_URL`) and `backend/.env` |

If `ULTRARAG_DATA_DIR` is set, the packaged app stores data in that directory
instead. If you customized `DATABASE_URL` in `backend/.env`, back up that
database instead of the default file.

Copying the SQLite database file while the application is stopped is a
sufficient backup for a rollback.

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

From the repository root:

```bash
# 1. Stop the running app
scripts/dev_down.sh

# 2. Back up the database and configuration
cp backend/skill_agent_loop.db backend/skill_agent_loop.db.bak
cp backend/.env backend/.env.bak

# 3. Update the code to the latest release
git fetch --tags origin
git checkout main
git pull origin main

# 4. Reinstall dependencies (versions may have changed)
backend/.venv/bin/python -m pip install -e "backend[dev]"
npm --prefix frontend-enterprise ci

# 5. Rebuild the frontend and restart
scripts/dev_up.sh --detach
```

On Windows PowerShell, use `.\scripts\dev_down.ps1`, `.\scripts\dev_up.ps1
--detach`, and `.\backend\.venv\Scripts\python.exe -m pip install -e
"backend[dev]"` for the equivalent steps.

`backend/.env` is ignored by git, so your model endpoints, API keys, and
`APP_SECRET` are preserved across upgrades. Do not regenerate `APP_SECRET`
during an upgrade — existing encrypted credentials depend on it.

To pin a specific release instead of tracking `main`, check out its tag,
e.g. `git checkout v0.5.3`.

## Verify the upgrade

1. Open the app and confirm `GET /api/health` responds (for source
   deployments, `curl http://127.0.0.1:5173/api/health`).
2. Sign in and confirm your digital employees, sessions, and knowledge bases
   are present.
3. If anything looks wrong, stop the app, restore the backed-up database
   file, and reinstall the previous release (or check out the previous tag).
