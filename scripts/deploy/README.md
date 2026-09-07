# Parallel Harness v3 deployment on 39

## Live layout

- Existing application: `http://39.102.210.77:10086/`, original `/data/StaffDeck` checkout and database.
- Parallel application: `http://39.102.210.77:10086/dsh/`.
- Independent instance directory: `/data/Staffdeck_Harnessv3`.
- New service: `staffdeck-harnessv3.service`, listening only on `127.0.0.1:10186`.
- New database: `/data/Staffdeck_Harnessv3/data/staffdeck.db`.
- Gateway: `parallel_gateway.py`, loaded by the existing service through the additive
  `/etc/systemd/system/ultraragv4-10086.service.d/zz-harnessv3.conf` override.
  Non-`/dsh` requests and the original application's lifespan still go to its original ASGI app.

## Isolation and data copy

`clone_harnessv3_instance.py` is a one-time, target-specific provisioning script. It refuses
to overwrite an existing destination database. It uses SQLite's online backup API, checks
integrity, copies workspace/attachment resources, and relocates structured resource paths.
Model keys remain encrypted; the encryption secret stays on the server in a mode-0640 env file.
The copy preserves existing users and passwords. It does not reset an admin password.

At creation, 141 users, 73 agents, 68 SOPs, 36 general skills, 144 knowledge documents and
7 model configurations were copied. The copy's 4 channel bindings and 22 scheduled tasks
were disabled/paused. Pending execution and delivery jobs were cancelled **only in the copy**.
External tool/model endpoints remain configured as in the source; review them before testing
actions that could write to external production systems.

The frontend is built with `--base=/dsh/`, uses the same prefix for API calls and router paths,
and isolates login tokens in `ultrarag_auth:/dsh`. The root deployment's token key is unchanged.

## Verification

- Both `/api/health` and `/dsh/api/health` must return OK through port 10086.
- `/dsh/` must stay below `/dsh` when redirected; JS, CSS and product images must load there.
- Linux sealed tests use a fake model and temporary database to exercise the real Harness
  process and the actual SSE worker. Do not use production credentials for these tests.
- Browser login and an actual model conversation require a valid copied user account.

## Rollback

The original override directory was copied to
`/data/Staffdeck_Harnessv3/deploy/legacy-dropins-before` before adding the new override.
To detach the new route, move **only** `zz-harnessv3.conf` out of the systemd override directory,
run `systemctl daemon-reload`, and restart `ultraragv4-10086.service` during an idle window.
The original ExecStart then takes effect. The independent new service and data can be retained
for investigation. Do not overwrite the legacy database with the test copy.
