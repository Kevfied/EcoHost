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
