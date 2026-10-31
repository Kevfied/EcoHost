# MC-EcoHost

A Minecraft server management suite — a FastAPI backend + web dashboard, wrapped
by a small Tauri desktop tool that owns backend lifecycle, config, and updates.

## The App (recommended)

Download `EcoHost_*_x64-setup.exe` from
[Releases](https://github.com/Kevfied/EcoHost/releases/latest) and install.

The tool is a small companion window — it is **not** the dashboard itself:

- **Overview** — backend status, Minecraft state, players, power mode
- **Server** — `ECOHOST_SERVER_DIR` folder picker + RCON settings
  (writes backend `config.json` **and** `server.properties`, then restarts
  the backend for you)
- **Updates** — signed self-updates from GitHub Releases
  (offer → download → install → auto-relaunch)
- **App** — autostart-on-login, data folder, backend log tail

Behaviors:

- Launching spawns the bundled backend on `:8000` and waits for `/health`.
- **Close hides to tray** — the backend keeps running. Tray menu:
  *Open EcoHost / Open Dashboard / Quit* — only Quit kills the backend.
- First launch enables login-autostart; toggle in the App tab.
- **Open Dashboard** opens `http://localhost:8000` in your browser —
  that is the full management UI (this is where you log in).
- Already running `python main.py`? The tool reuses that backend and won't
  kill it on exit — it only manages what it spawned.

### Where state lives

| Thing | Location |
|---|---|
| Tool config (`server_dir`, autostart flag) | `%LOCALAPPDATA%\EcoHost\tool.json` |
| Backend settings incl. JWT secret + RCON | `%LOCALAPPDATA%\EcoHost\config.json` |
| Users (auth.db), player/uptime stats | `%LOCALAPPDATA%\EcoHost\` |
| Backend + install logs | `%LOCALAPPDATA%\EcoHost\logs\` |
| Update store + staged downloads | `%LOCALAPPDATA%\EcoHost\appupdate.json`, `updates-dl\` |

All of it survives app updates — the installer only touches the install dir.

## From Source (browser mode, no app)

```powershell
pip install -r requirements.txt
python main.py
```

Dashboard: `http://localhost:8000` — do **not** open `web/dashboard.html` or
`web/login.html` as files; they're served by the backend.

In source mode everything lives under `data/` in the repo instead of
`%LOCALAPPDATA%\EcoHost`. Point at your server:

```powershell
$env:ECOHOST_SERVER_DIR = "C:\Path\To\Your\Minecraft\Server"
```

## Authentication

- First run creates `admin` / `admin` — you're forced to change it on login.
- The default admin only spawns when the auth DB has no users. If
  `admin`/`admin` fails, delete `auth.db` (in `data\` from source, or
  `%LOCALAPPDATA%\EcoHost\` for the app) and restart — a fresh one is created.
- Login rate limiting: 5 failed attempts → 15 minute lockout.

### Roles

- **Admin** — everything: control, settings, users
- **Moderator** — server control, player management, logs
- **Viewer Plus** — can start server, dashboard + logs
- **Viewer** — read-only dashboard + logs

## API (subset)

| Endpoint | Method | Description | Auth |
|---|---|---|---|
| `/health` | GET | health check | No |
| `/public-status` | GET | non-sensitive status (mc state, players, uptime, power mode) | No |
| `/status` | GET | full server status | Yes |
| `/power/start` `/power/stop` | POST | server control | Moderator+ |
| `/power/mode` | POST | set power plan | Moderator+ |
| `/logs`, `/ecohost-logs` | GET | log tails | permissioned |
| `/settings` | GET/POST | settings (GET returns `rcon_password_set`, never the secret) | Admin |
| `/users` | GET/POST | user management | Admin |
| `/auth/login` `/auth/register` `/auth/verify` `/auth/change-password` `/auth/me` | POST/GET | auth | varies |
| `/ws` | WebSocket | real-time status | Yes |

## Backend settings (`config.json`)

`auto_shutdown_enabled`, `auto_shutdown_duration` (seconds),
`auto_start_on_ping`, `power_mode_scheduling_enabled`,
`high_performance_start`/`end`, `ecohost_precision_enabled`,
`rcon_enabled`, `rcon_port`, `rcon_password` — set via the dashboard, the
app's Server tab, or edit the file and restart the backend.

`server.properties` needs `enable-rcon=true` + matching `rcon.port`/
`rcon.password` for RCON to work — the app's Server tab writes these for
you when the file exists.

## Development

```powershell
npm install              # once
npm run dev              # vite on :5173
cd src-tauri; cargo run  # app (debug) — spawns sidecar, python fallback
```

Checks:

- `npx tsc --noEmit` — frontend typecheck
- `cd src-tauri && cargo build` — Rust
- `cd src-tauri && cargo run --example sandbox` — feature matrix
  (updater, config writers, backend endpoints; must end **0 failure(s)**)

`tauri dev` is intentionally **not** used: the CLI waits for `devUrl` before
launching, but our backend starts inside the app — use `cargo run`.

## Releasing

```powershell
scripts\release.ps1 -Bump patch -Notes "what changed"
```

Bumps the version everywhere → rebuilds the PyInstaller sidecar (skipped when
Python inputs are unchanged, `-ForceBackend` to force) → `tauri build`
(NSIS only) → signs `latest.json` (minisign) → publishes a GitHub release
after an explicit `y`. Clients pick it up on their next update check.

- Signing key: `%LOCALAPPDATA%\EcoHost\update-sign.key` — **back it up**;
  if it's lost, shipped clients can't verify future updates.
- The baked-in public key lives in `src-tauri/src/updater.rs`
  (`UPDATE_PUBKEY`).
- Trust model: the signed manifest is the root — payload is bound by
  sha256 inside it; serving host doesn't matter.
- `scripts\cdp.mjs` drives WebView2 over CDP for headless UI checks
  (launch app with `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222`).

## Security notes

- Change the default admin password on first login.
- `data/`, `%LOCALAPPDATA%\EcoHost\`, and the signing key are gitignored —
  keep them that way. Old repo history once contained a real RCON
  password — if that was ever exposed on a live server, rotate it.
- The dashboard never returns `rcon_password` — only `rcon_password_set`.

## Troubleshooting

- **Backend won't come up** — check `%LOCALAPPDATA%\EcoHost\logs\backend.log`
  (the sidecar is silent by design; that file is its only output).
- **admin/admin fails** — stale `auth.db`; delete it, restart (see above).
- **Server won't start** — verify `server_dir` in the Server tab points at
  the folder containing `run.bat`; check the backend log tail in the App tab.
- **Duplicate app in tray** — should be impossible (single-instance guard);
  the second launch raises the existing window instead.
- **Install/update stalled** — `%LOCALAPPDATA%\EcoHost\logs\install.log`
  traces every step; a timed-out install restores the previous exe.
