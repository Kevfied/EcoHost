# EcoHost

Tauri v2 companion tool + Python FastAPI backend. Rust shell (`src-tauri`),
React+TS tool UI (`src`), Vite. The backend stays Python — the app supervises
it, it is NOT a webview of the dashboard.

## Verify

- `cd src-tauri && cargo run --example sandbox` — feature matrix (updater,
  config writers, backend endpoints); must end **0 failure(s)**, self-cleans.
- `npx tsc --noEmit` — frontend typecheck.
- `cd src-tauri && cargo build` — Rust.
- `python -m py_compile` on edited Python modules.
- `tauri dev` is not used — the CLI waits for devUrl before launch, but the
  backend starts inside the app. Use `cargo run` (vite on :5173 for the
  frontend, or rely on the built `dist/` for release runs).

## Architecture rules

- **Two data roots.** Frozen backend: `%LOCALAPPDATA%\EcoHost` (`Modules/
  config.py DATA_DIR`). Source backend (`python main.py`): `data/` in the
  repo. Bundled assets come from `_MEIPASS`. Never write under BASE_DIR
  when frozen — it's a temp dir that vanishes on exit.
- **`backend::backend_config_path()`** resolves which of those config.json
  files the *running* backend owns — existence-check order, sidecar first.
- Backend lifecycle: `ensure_running` reuses an already-healthy :8000
  (never kills what it didn't spawn) → sidecar → python fallback. Kill is
  `taskkill /T` — a PyInstaller child survives plain kill.
- Close hides to tray; only tray **Quit** kills the backend.
- Single-instance plugin raises the existing window on second launch.

## Updates (GitHub Releases, standalone)

`src-tauri/src/updater.rs` + `scripts/release.ps1` — ported from mc-modder's
LAN model; transport is GitHub instead of peer-seeding.

- Trust = detached **minisign signature on `latest.json`**. Payload bound
  by sha256 inside the signed manifest.
- `UPDATE_PUBKEY` in updater.rs is baked in; the secret key lives at
  `%LOCALAPPDATA%\EcoHost\update-sign.key` — never in the repo.
- `release.ps1` bumps → sidecar (skipped when Python inputs hash-match,
  `-ForceBackend` overrides) → `tauri build` → sign → `gh release create`.
  Explicit `y` gate before publish — nothing goes live without it.
- NSIS-only bundling (`bundle.targets`); update.cmd's `/S` is NSIS-specific.
- `set_release_base()` redirects fetches — sandbox uses it for local HTTP;
  it's also the hook if LAN seeding is ever wanted.
- `write_update_cmd` is split from `install_app_update` so tests can assert
  the script without spawning it (spawning would relaunch the test exe).

## Conventions

- Monochrome dark UI, sharp 3px edges, monospace; left rail nav
  Overview | Server | Updates | App. `#0a0a0a` bg, `#e8e8e8` ink.
- Async Tauri commands; never hold a std MutexGuard across `.await`.
- All persisted structs use `#[serde(default)]`.
- `web/vendor/` serves tailwind + chart.js locally — no CDN in the app.
- Never commit secrets: `data/`, `src-tauri/binaries/`, build output and
  the signing key stay ignored. `GET /settings` returns `rcon_password_set`,
  never the secret. An old RCON password lives in git history — treat as
  compromised, never reuse.
