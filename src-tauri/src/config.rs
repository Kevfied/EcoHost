//! Tool configuration — the things the web dashboard can't touch because
//! they belong to *this* process: where the Minecraft server lives, where
//! the backend's writable state goes, whether to start on login.
//!
//! Persisted to %LOCALAPPDATA%\EcoHost\tool.json. All fields default so
//! older stores never break on load.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::OnceLock;

static APP_DIR: OnceLock<PathBuf> = OnceLock::new();

/// Base for all tool state: %LOCALAPPDATA%\EcoHost — same root the frozen
/// backend writes to (Modules/config.py DATA_DIR).
pub fn app_dir() -> PathBuf {
    APP_DIR
        .get_or_init(|| {
            let base = std::env::var("LOCALAPPDATA")
                .map(PathBuf::from)
                .unwrap_or_else(|_| dirs::home_dir().unwrap_or_else(|| PathBuf::from(".")));
            base.join("EcoHost")
        })
        .clone()
}

/// Tools/tests may anchor the dir elsewhere — first call wins.
pub fn set_app_dir(p: PathBuf) {
    let _ = APP_DIR.set(p);
}

fn store_path() -> PathBuf {
    app_dir().join("tool.json")
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct ToolConfig {
    /// Minecraft server directory — passed to the backend as
    /// ECOHOST_SERVER_DIR on spawn.
    pub server_dir: String,
    /// First-run flag: we auto-enable login autostart once, then respect
    /// whatever the user toggles in the App tab.
    pub autostart_configured: bool,
}

pub fn load() -> ToolConfig {
    std::fs::read_to_string(store_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

pub fn save(cfg: &ToolConfig) -> Result<(), String> {
    let j = serde_json::to_string_pretty(cfg).map_err(|e| e.to_string())?;
    let f = store_path();
    if let Some(p) = f.parent() {
        std::fs::create_dir_all(p).map_err(|e| e.to_string())?;
    }
    // Atomic write — never leave a half-written store.
    let tmp = f.with_extension("tmp");
    std::fs::write(&tmp, j).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, &f).map_err(|e| e.to_string())
}

/// Merge RCON keys into the backend's data/config.json — preserving every
/// other key (jwt_secret, power settings, …). The backend reads this file
/// at startup, so the caller restarts it after writing.
pub fn write_backend_rcon(
    config_path: &std::path::Path,
    enabled: bool,
    port: u16,
    password: &str,
) -> Result<(), String> {
    let mut v: serde_json::Value = std::fs::read_to_string(config_path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_else(|| serde_json::json!({}));
    if !v.is_object() {
        v = serde_json::json!({});
    }
    let o = v.as_object_mut().unwrap();
    o.insert("rcon_enabled".into(), serde_json::json!(enabled));
    o.insert("rcon_port".into(), serde_json::json!(port));
    if !password.is_empty() {
        // Empty field = keep whatever the backend already has — never
        // clobber a secret with a blank.
        o.insert("rcon_password".into(), serde_json::json!(password));
    }
    if let Some(p) = config_path.parent() {
        std::fs::create_dir_all(p).map_err(|e| e.to_string())?;
    }
    let tmp = config_path.with_extension("tmp");
    std::fs::write(&tmp, serde_json::to_string_pretty(&v).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, config_path).map_err(|e| e.to_string())
}

/// Patch server.properties with the RCON keys. Returns a human summary.
/// A missing file is NOT an error — brand-new servers create it on first
/// boot; we report "skipped" instead of fabricating one the server might
/// not merge cleanly.
pub fn update_server_properties(
    server_dir: &std::path::Path,
    enabled: bool,
    port: u16,
    password: &str,
) -> Result<String, String> {
    let path = server_dir.join("server.properties");
    if !path.is_file() {
        return Ok("server.properties not found — RCON saved to the backend; re-save once the server generates the file".into());
    }
    let body = std::fs::read_to_string(&path).map_err(|e| e.to_string())?;
    let want: Vec<(&str, String)> = vec![
        ("enable-rcon", enabled.to_string()),
        ("rcon.port", port.to_string()),
    ];
    // Password only when the caller actually set one — same don't-clobber
    // rule as the backend config.
    let mut want = want;
    if !password.is_empty() {
        want.push(("rcon.password", password.to_string()));
    }
    let mut lines: Vec<String> = body.lines().map(|l| l.to_string()).collect();
    for (k, v) in &want {
        let prefix = format!("{k}=");
        if let Some(l) = lines.iter_mut().find(|l| l.starts_with(&prefix)) {
            *l = format!("{k}={v}");
        } else {
            lines.push(format!("{k}={v}"));
        }
    }
    let mut out = lines.join("\n");
    if body.ends_with('\n') {
        out.push('\n');
    }
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, out).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, &path).map_err(|e| e.to_string())?;
    Ok(format!(
        "server.properties updated (enable-rcon={enabled}, port={port}{})",
        if password.is_empty() { "" } else { ", password set" }
    ))
}
