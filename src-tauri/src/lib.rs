pub mod backend;
pub mod config;
pub mod updater;

use serde::Serialize;
use tauri::Manager;

struct AppState {
    http: reqwest::Client,
}

#[derive(Serialize)]
struct Overview {
    /// Backend answered /health.
    backend_up: bool,
    /// This app spawned the backend (vs. reusing one already running).
    managed: bool,
    /// Server dir our spawned backend uses, if we spawned it.
    spawned_dir: Option<String>,
    /// Pass-through of GET /public-status, or null when backend is down.
    public_status: serde_json::Value,
    /// Path to the backend log file the tool tails.
    backend_log: String,
    /// Tool's persistent app dir (%LOCALAPPDATA%\EcoHost).
    app_dir: String,
}

// ---------------------------------------------------------------- backend

#[tauri::command]
async fn get_overview(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
) -> Result<Overview, String> {
    let up = backend::health_ok();
    let url = format!("http://localhost:{}/public-status", backend::BACKEND_PORT);
    let public_status = if up {
        match state
            .http
            .get(&url)
            .timeout(std::time::Duration::from_secs(3))
            .send()
            .await
        {
            Ok(r) => r
                .json::<serde_json::Value>()
                .await
                .unwrap_or(serde_json::Value::Null),
            Err(_) => serde_json::Value::Null,
        }
    } else {
        serde_json::Value::Null
    };
    Ok(Overview {
        backend_up: up,
        managed: app
            .try_state::<backend::BackendState>()
            .map(|s| s.is_managed())
            .unwrap_or(false),
        spawned_dir: backend::spawned_dir(&app),
        public_status,
        backend_log: backend::backend_log_path().display().to_string(),
        app_dir: config::app_dir().display().to_string(),
    })
}

#[tauri::command]
async fn restart_backend(app: tauri::AppHandle) -> Result<String, String> {
    let app2 = app.clone();
    let r = tauri::async_runtime::spawn_blocking(move || backend::restart(&app2))
        .await
        .map_err(|e| e.to_string())?;
    Ok(r.to_string())
}

#[tauri::command]
async fn stop_backend(app: tauri::AppHandle) -> Result<String, String> {
    backend::kill(&app);
    Ok("stopped".into())
}

#[tauri::command]
async fn tail_backend_log(lines: usize) -> Result<Vec<String>, String> {
    let p = backend::backend_log_path();
    let text = std::fs::read_to_string(&p).unwrap_or_default();
    let v: Vec<String> = text.lines().map(|l| l.to_string()).collect();
    Ok(v.into_iter().rev().take(lines).collect::<Vec<_>>()
        .into_iter().rev().collect())
}

// ---------------------------------------------------------------- config

#[tauri::command]
async fn get_tool_config(_app: tauri::AppHandle) -> Result<config::ToolConfig, String> {
    Ok(config::load())
}

/// Save config. `needs_restart` is true when the server dir changed and a
/// backend is already running with the old one.
#[tauri::command]
async fn set_tool_config(
    cfg: config::ToolConfig,
) -> Result<bool, String> {
    let old = config::load();
    let dir_changed = old.server_dir != cfg.server_dir;
    config::save(&cfg)?;
    let needs_restart = dir_changed && backend::health_ok();
    Ok(needs_restart)
}

#[tauri::command]
async fn pick_server_dir(app: tauri::AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let f = app
        .dialog()
        .file()
        .set_title("Select Minecraft server folder")
        .blocking_pick_folder();
    Ok(f.and_then(|p| p.into_path().ok()).map(|p| p.display().to_string()))
}

// ---------------------------------------------------------------- openers

#[tauri::command]
async fn open_dashboard(app: tauri::AppHandle) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    app.opener()
        .open_url(
            format!("http://localhost:{}", backend::BACKEND_PORT),
            None::<&str>,
        )
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn open_dir(path: String, app: tauri::AppHandle) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    if path.trim().is_empty() {
        return Err("empty path".into());
    }
    app.opener()
        .open_path(&path, None::<&str>)
        .map_err(|e| e.to_string())
}

// ---------------------------------------------------------------- updates

#[tauri::command]
async fn app_update_status(
    state: tauri::State<'_, AppState>,
) -> Result<updater::AppUpdateStatus, String> {
    Ok(updater::app_update_status(&state.http, &config::app_dir()).await)
}

#[tauri::command]
async fn download_app_update(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
) -> Result<String, String> {
    let http = state.http.clone();
    let dir = config::app_dir();
    tauri::async_runtime::spawn(async move {
        use tauri::Emitter;
        updater::download_app_update(&http, &dir, move |done, total| {
            let _ = app.emit(
                "update-progress",
                serde_json::json!({ "done": done, "total": total }),
            );
        })
        .await
        .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn cancel_app_update() -> Result<(), String> {
    updater::cancel_download();
    Ok(())
}

#[tauri::command]
async fn install_app_update(app: tauri::AppHandle) -> Result<String, String> {
    // The spawned update.cmd waits for this process to die before
    // overwriting our exe — kill the backend, then exit after replying.
    let msg = updater::install_app_update(&config::app_dir()).map_err(|e| e.to_string())?;
    let app2 = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(400));
        backend::kill(&app2);
        app2.exit(0);
    });
    Ok(msg)
}

#[tauri::command]
async fn skip_app_version(version: String) -> Result<(), String> {
    updater::skip_version(&config::app_dir(), &version);
    Ok(())
}

#[tauri::command]
async fn unskip_app_version() -> Result<(), String> {
    updater::unskip_version(&config::app_dir());
    Ok(())
}

#[tauri::command]
async fn ack_app_update() -> Result<(), String> {
    updater::ack_update_banner(&config::app_dir());
    Ok(())
}

// ---------------------------------------------------------------- run

pub fn run() {
    let app_dir = config::app_dir();
    updater::init(&app_dir);
    updater::launch_purge(&app_dir);

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_autostart::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .manage(backend::BackendState::new())
        .manage(AppState {
            http: reqwest::Client::builder()
                .user_agent(concat!("ecohost/", env!("CARGO_PKG_VERSION")))
                .build()
                .unwrap_or_default(),
        })
        .invoke_handler(tauri::generate_handler![
            get_overview,
            restart_backend,
            stop_backend,
            tail_backend_log,
            get_tool_config,
            set_tool_config,
            pick_server_dir,
            open_dashboard,
            open_dir,
            app_update_status,
            download_app_update,
            cancel_app_update,
            install_app_update,
            skip_app_version,
            unskip_app_version,
            ack_app_update,
        ])
        .setup(|app| {
            // First launch: enable login autostart by default. After that
            // the App-tab toggle is the source of truth.
            {
                use tauri_plugin_autostart::ManagerExt;
                let mut cfg = config::load();
                if !cfg.autostart_configured {
                    let _ = app.autolaunch().enable();
                    cfg.autostart_configured = true;
                    let _ = config::save(&cfg);
                }
            }

            // Tray: close hides to tray (backend keeps running); only an
            // explicit Quit really exits.
            {
                use tauri::{
                    menu::{MenuBuilder, MenuItemBuilder},
                    tray::TrayIconBuilder,
                };
                let menu = MenuBuilder::new(app)
                    .item(&MenuItemBuilder::with_id("open", "Open EcoHost").build(app)?)
                    .item(&MenuItemBuilder::with_id("dash", "Open Dashboard").build(app)?)
                    .separator()
                    .item(&MenuItemBuilder::with_id("quit", "Quit").build(app)?)
                    .build()?;
                TrayIconBuilder::new()
                    .icon(app.default_window_icon().unwrap().clone())
                    .tooltip("EcoHost")
                    .menu(&menu)
                    .on_menu_event(|app, ev| match ev.id().as_ref() {
                        "open" => {
                            if let Some(w) = app.get_webview_window("main") {
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                        "dash" => {
                            use tauri_plugin_opener::OpenerExt;
                            let _ = app.opener().open_url(
                                format!("http://localhost:{}", backend::BACKEND_PORT),
                                None::<&str>,
                            );
                        }
                        "quit" => {
                            backend::kill(app);
                            app.exit(0);
                        }
                        _ => {}
                    })
                    .build(app)?;
            }

            // Window close → hide to tray, don't exit.
            if let Some(win) = app.get_webview_window("main") {
                let w = win.clone();
                win.on_window_event(move |e| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = e {
                        api.prevent_close();
                        let _ = w.hide();
                    }
                });
                let _ = win.show();
                let _ = win.set_focus();
            }

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                // Backend comes up in the background — the UI polls
                // get_overview and shows "starting…" until health lands.
                backend::ensure_running(&handle);
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| match event {
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit => {
                backend::kill(app)
            }
            _ => {}
        });
}
