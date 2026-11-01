//! sandbox — heavy feature-matrix test for the EcoHost app shell.
//!
//! Covers: manifest gate branches, signature forgery, tool config roundtrip,
//! backend lifecycle (python + bundled sidecar), HTTP endpoint sanity
//! (public routes 200, authed routes 401), and the updater end-to-end
//! (throwaway key → signed manifest → local HTTP → download → staged install
//! → update.cmd assertions — the installer itself is never executed).
//!
//!   cargo run --example sandbox
//!
//! Must end with 0 failure(s) and self-cleans its temp dir.

use ecohost_lib::{backend, config, updater};
use serde_json::json;
use sha2::Digest;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use std::sync::atomic::{AtomicUsize, Ordering};

static PASS: AtomicUsize = AtomicUsize::new(0);
static FAIL: AtomicUsize = AtomicUsize::new(0);

fn check(name: &str, cond: bool) {
    if cond {
        PASS.fetch_add(1, Ordering::Relaxed);
        println!("  PASS  {name}");
    } else {
        FAIL.fetch_add(1, Ordering::Relaxed);
        println!("  FAIL  {name}");
    }
}

fn check_err(name: &str, r: &anyhow::Result<impl std::fmt::Debug>, needle: &str) {
    match r {
        Err(e) => {
            let ok = e.to_string().contains(needle);
            check(&format!("{name} (got: {e})"), ok);
        }
        Ok(v) => check(&format!("{name} (unexpected ok: {v:?})"), false),
    }
}

fn tmp_root() -> PathBuf {
    let p = std::env::temp_dir().join(format!("ecohost-sandbox-{}", std::process::id()));
    let _ = std::fs::create_dir_all(&p);
    p
}

fn manifest(
    app: &str,
    version: &str,
    channel: &str,
    arch: &str,
    file: &str,
) -> updater::UpdateManifest {
    updater::UpdateManifest {
        app: app.into(),
        version: version.into(),
        channel: channel.into(),
        arch: arch.into(),
        notes: "test notes".into(),
        pub_date: String::new(),
        file: file.into(),
        sha256: "00".repeat(32),
        size: 1,
    }
}

// ---------------------------------------------------------------- gate

fn test_gate() {
    println!("[gate] manifest gate branches");
    let cur = env!("CARGO_PKG_VERSION");
    let arch = std::env::consts::ARCH;
    let next = "9.9.9";

    check(
        "accepts valid newer manifest",
        updater::gate_manifest(
            &manifest("com.ecohost.app", next, "stable", arch, "ok.exe"),
            cur,
            None,
        )
        .is_ok(),
    );
    check_err(
        "rejects wrong app id",
        &updater::gate_manifest(
            &manifest("com.other.app", next, "stable", arch, "ok.exe"),
            cur,
            None,
        ),
        "different app",
    );
    check_err(
        "rejects wrong channel",
        &updater::gate_manifest(
            &manifest("com.ecohost.app", next, "beta", arch, "ok.exe"),
            cur,
            None,
        ),
        "channel",
    );
    check_err(
        "rejects wrong arch",
        &updater::gate_manifest(
            &manifest("com.ecohost.app", next, "stable", "wrongarch", "ok.exe"),
            cur,
            None,
        ),
        "architecture",
    );
    check_err(
        "rejects same version",
        &updater::gate_manifest(
            &manifest("com.ecohost.app", cur, "stable", arch, "ok.exe"),
            cur,
            None,
        ),
        "not newer",
    );
    check_err(
        "rejects older version",
        &updater::gate_manifest(
            &manifest("com.ecohost.app", "0.0.1", "stable", arch, "ok.exe"),
            cur,
            None,
        ),
        "not newer",
    );
    check_err(
        "rejects skipped version",
        &updater::gate_manifest(
            &manifest("com.ecohost.app", next, "stable", arch, "ok.exe"),
            cur,
            Some(next),
        ),
        "skipped",
    );
    for bad in ["../x.exe", "a/b.exe", "a\\b.exe", "a:b.exe", "", "..x.exe"] {
        check_err(
            &format!("rejects bad filename {bad:?}"),
            &updater::gate_manifest(
                &manifest("com.ecohost.app", next, "stable", arch, bad),
                cur,
                None,
            ),
            "filename",
        );
    }
}

// ---------------------------------------------------------------- sig

fn test_sig() {
    println!("[sig] minisign verify paths");
    let kp = minisign::KeyPair::generate_unencrypted_keypair().unwrap();
    let pubkey = kp.pk.to_base64();
    let msg = b"{\"app\":\"com.ecohost.app\",\"version\":\"9.9.9\"}";
    let sig = minisign::sign(
        Some(&kp.pk),
        &kp.sk,
        std::io::Cursor::new(msg.to_vec()),
        None,
        None,
    )
    .unwrap()
    .to_string();

    check(
        "good signature verifies",
        updater::verify_manifest_sig(msg, &sig, &pubkey).is_ok(),
    );
    check_err(
        "tampered manifest rejected",
        &updater::verify_manifest_sig(b"{\"app\":\"com.evil.app\"}", &sig, &pubkey),
        "verify failed",
    );
    let kp2 = minisign::KeyPair::generate_unencrypted_keypair().unwrap();
    check_err(
        "wrong pubkey rejected",
        &updater::verify_manifest_sig(msg, &sig, &kp2.pk.to_base64()),
        "verify failed",
    );
    check_err(
        "garbage sig rejected",
        &updater::verify_manifest_sig(msg, "not-a-sig", &pubkey),
        "",
    );
    check_err(
        "garbage pubkey rejected",
        &updater::verify_manifest_sig(msg, &sig, "not-a-key"),
        "pubkey",
    );
}

// ---------------------------------------------------------------- config

fn test_config(dir: &Path) {
    println!("[config] tool store roundtrip");
    config::set_app_dir(dir.to_path_buf());
    let cfg = config::ToolConfig {
        server_dir: r"C:\mc\server".into(),
        autostart_configured: false,
    };
    check("save ok", config::save(&cfg).is_ok());
    let back = config::load();
    check("roundtrip server_dir", back.server_dir == cfg.server_dir);
    // Corrupt file → defaults, never a crash.
    let f = dir.join("tool.json");
    std::fs::write(&f, "{not json").unwrap();
    let d = config::load();
    check("corrupt store falls back to defaults", d.server_dir.is_empty());
    // Reset to an empty server_dir — backend spawns later in this run read
    // the store and would otherwise inherit a bogus ECOHOST_SERVER_DIR.
    let _ = config::save(&config::ToolConfig {
        server_dir: String::new(),
        autostart_configured: false,
    });

    // server.properties patcher
    let srv = dir.join("srv");
    std::fs::create_dir_all(&srv).unwrap();
    std::fs::write(
        srv.join("server.properties"),
        "motd=test\nenable-rcon=false\nrcon.port=1234\n",
    )
    .unwrap();
    let msg = config::update_server_properties(&srv, true, 25575, "sekret").unwrap();
    check("server.properties patch ok", msg.contains("updated"));
    let body = std::fs::read_to_string(srv.join("server.properties")).unwrap();
    check(
        "props: rcon enabled, port replaced, password added, motd kept",
        body.contains("enable-rcon=true")
            && body.contains("rcon.port=25575")
            && body.contains("rcon.password=sekret")
            && body.contains("motd=test"),
    );
    // Blank password must NOT clobber the existing one.
    config::update_server_properties(&srv, true, 25575, "").unwrap();
    let body = std::fs::read_to_string(srv.join("server.properties")).unwrap();
    check("blank password leaves rcon.password alone", body.contains("rcon.password=sekret"));
    // Missing file = graceful skip, not an error.
    let missing = config::update_server_properties(&dir.join("nope"), true, 25575, "x").unwrap();
    check("missing server.properties → skipped msg", missing.contains("not found"));

    // backend config.json merge — preserves other keys incl. secrets
    let cfgp = dir.join("beconfig").join("config.json");
    std::fs::create_dir_all(cfgp.parent().unwrap()).unwrap();
    std::fs::write(&cfgp, r#"{"jwt_secret":"keepme","auto_shutdown_enabled":true}"#).unwrap();
    config::write_backend_rcon(&cfgp, true, 25575, "pw1").unwrap();
    let v: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&cfgp).unwrap()).unwrap();
    check(
        "config merge: rcon keys set + jwt_secret preserved",
        v["rcon_enabled"] == json!(true)
            && v["rcon_port"] == json!(25575)
            && v["rcon_password"] == json!("pw1")
            && v["jwt_secret"] == json!("keepme"),
    );
    config::write_backend_rcon(&cfgp, true, 25575, "").unwrap();
    let v: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&cfgp).unwrap()).unwrap();
    check("blank password keeps stored rcon_password", v["rcon_password"] == json!("pw1"));
}

// ---------------------------------------------------------------- backend

fn repo_root() -> PathBuf {
    for anc in std::env::current_exe().unwrap().ancestors() {
        if anc.join("main.py").exists() {
            return anc.to_path_buf();
        }
    }
    panic!("repo root (main.py) not found above sandbox exe");
}

struct KillGuard(Option<Child>);
impl Drop for KillGuard {
    fn drop(&mut self) {
        if let Some(c) = &mut self.0 {
            // taskkill /T — a PyInstaller child survives a plain kill of its
            // parent (same rule as the app's backend shutdown).
            let pid = c.id();
            if let Ok(mut k) = Command::new("taskkill")
                .args(["/PID", &pid.to_string(), "/T", "/F"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
            {
                let _ = k.wait();
            }
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}

fn spawn_headless(cmd: &mut Command) -> Option<Child> {
    cmd.stdin(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }
    cmd.spawn().ok()
}

/// Spawn a backend proc, wait for health, run the endpoint matrix, kill.
fn test_backend_endpoints(tag: &str, rt: &tokio::runtime::Runtime) {
    println!("[backend:{tag}] lifecycle + endpoints");
    let http = reqwest::Client::new();
    if !backend::wait_healthy(Duration::from_secs(50)) {
        check(&format!("{tag}: backend became healthy"), false);
        return;
    }
    check(&format!("{tag}: backend became healthy"), true);

    let base = format!("http://localhost:{}", backend::BACKEND_PORT);
    let get = |p: &str| {
        let h = &http;
        let u = format!("{base}{p}");
        rt.block_on(async move { h.get(&u).send().await })
    };

    check(
        "GET /health 200",
        get("/health")
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
    );
    let ps = get("/public-status").and_then(|r| {
        rt.block_on(async move { r.json::<serde_json::Value>().await })
    });
    match ps {
        Ok(v) => {
            check("GET /public-status 200", true);
            check(
                "public-status has mc_state + players_online + power_mode",
                v["mc_state"].is_string()
                    && v["players_online"].is_number()
                    && v["power_mode"].is_string(),
            );
        }
        Err(_) => check("GET /public-status 200", false),
    }
    check(
        "GET /login.html 200",
        get("/login.html")
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
    );
    check(
        "GET /status unauthenticated → 401",
        get("/status")
            .map(|r| r.status().as_u16() == 401)
            .unwrap_or(false),
    );
    check(
        "GET /settings unauthenticated → 401",
        get("/settings")
            .map(|r| r.status().as_u16() == 401)
            .unwrap_or(false),
    );
    check(
        "GET /settings leaks no rcon password",
        get("/settings")
            .map(|r| {
                rt.block_on(async move { r.text().await })
                    .map(|t| !t.contains("rcon_password\""))
                    .unwrap_or(false)
            })
            .unwrap_or(false),
    );
}

fn test_backend_python(rt: &tokio::runtime::Runtime) {
    let root = repo_root();
    let mut cmd = Command::new("python");
    cmd.arg("main.py").current_dir(&root);
    let Some(child) = spawn_headless(&mut cmd) else {
        check("python backend spawned", false);
        return;
    };
    check("python backend spawned", true);
    let _g = KillGuard(Some(child));
    test_backend_endpoints("python", rt);
}

/// Snapshot the repo data/ dir and restore it on drop — backend tests write
/// auth.db/config.json/stats there and must leave "like it was never there".
struct DataGuard(PathBuf, PathBuf);
impl DataGuard {
    fn snapshot() -> Self {
        let data = repo_root().join("data");
        let snap = tmp_root().join("data-snapshot");
        if data.is_dir() {
            let _ = copy_dir(&data, &snap);
        }
        DataGuard(data, snap)
    }
}
impl Drop for DataGuard {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
        if self.1.is_dir() {
            let _ = copy_dir(&self.1, &self.0);
        }
    }
}

fn copy_dir(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for e in std::fs::read_dir(src)? {
        let e = e?;
        let to = dst.join(e.file_name());
        if e.file_type()?.is_dir() {
            copy_dir(&e.path(), &to)?;
        } else {
            std::fs::copy(e.path(), &to)?;
        }
    }
    Ok(())
}

/// Full authed matrix against a temp SERVER_DIR: login, perm gates, log-line
/// parse → player tracking, maintenance, emergency, backups, public-status
/// leak check, ping-listener arming.
fn test_backend_full(rt: &tokio::runtime::Runtime) {
    println!("[backend:full] authed feature matrix (temp server dir)");

    // Temp MC server dir with a log file + world folders the watcher/backup see
    let srv = tmp_root().join("mc-server");
    let logs_dir = srv.join("logs");
    std::fs::create_dir_all(&logs_dir).unwrap();
    std::fs::write(
        logs_dir.join("latest.log"),
        "[12:00:00] [Server thread/INFO]: Starting minecraft server version 1.21\n[12:00:01] [Server thread/INFO]: Done (1.0s)! For help, type \"help\"\n",
    )
    .unwrap();
    for dim in ["world", "world_nether"] {
        std::fs::create_dir_all(srv.join(dim).join("region")).unwrap();
        std::fs::write(srv.join(dim).join("level.dat"), b"fake").unwrap();
        std::fs::write(srv.join(dim).join("region").join("r.0.0.mca"), vec![0u8; 4096]).unwrap();
    }

    let _data_guard = DataGuard::snapshot();

    let root = repo_root();
    let mut cmd = Command::new("python");
    cmd.arg("main.py")
        .current_dir(&root)
        .env("ECOHOST_SERVER_DIR", &srv);
    let Some(child) = spawn_headless(&mut cmd) else {
        check("full: backend spawned", false);
        return;
    };
    check("full: backend spawned", true);
    let _g = KillGuard(Some(child));

    if !backend::wait_healthy(Duration::from_secs(50)) {
        check("full: backend became healthy", false);
        return;
    }
    check("full: backend became healthy", true);

    let http = reqwest::Client::new();
    let base = format!("http://localhost:{}", backend::BACKEND_PORT);

    // -- public-status leak check -----------------------------------------
    let ps = rt
        .block_on(http.get(format!("{base}/public-status")).send())
        .ok()
        .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok());
    match ps {
        Some(v) => {
            check(
                "public-status leaks no players/server_dir/cpu/ram",
                v.get("players").is_none()
                    && v.get("server_dir").is_none()
                    && v.get("cpu_usage").is_none()
                    && v.get("ram_usage").is_none(),
            );
            check("public-status keeps count", v["players_online"].is_number());
        }
        None => check("public-status responded", false),
    }

    // -- admin login → cookie ----------------------------------------------
    let cookie = rt
        .block_on(
            http.post(format!("{base}/auth/login"))
                .json(&json!({"username": "admin", "password": "admin"}))
                .send(),
        )
        .ok()
        .and_then(|r| {
            check("admin login 200", r.status().as_u16() == 200);
            r.headers()
                .get("set-cookie")
                .and_then(|v| v.to_str().ok())
                .map(|s| s.split(';').next().unwrap_or("").to_string())
        });
    let Some(admin_cookie) = cookie.filter(|c| c.contains("access_token=")) else {
        check("admin login set access_token cookie", false);
        return;
    };
    check("admin login set access_token cookie", true);

    let get_a = |p: &str| {
        rt.block_on(
            http.get(format!("{base}{p}"))
                .header("Cookie", &admin_cookie)
                .send(),
        )
        .ok()
    };
    let post_a = |p: &str, body: serde_json::Value| {
        rt.block_on(
            http.post(format!("{base}{p}"))
                .header("Cookie", &admin_cookie)
                .json(&body)
                .send(),
        )
        .ok()
    };
    let del_a = |p: &str| {
        rt.block_on(
            http.delete(format!("{base}{p}"))
                .header("Cookie", &admin_cookie)
                .send(),
        )
        .ok()
    };

    // -- authed /status carries the new fields ------------------------------
    let st = get_a("/status")
        .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok());
    match st {
        Some(v) => {
            check("GET /status authed 200", true);
            check(
                "status has tps/avg_tick/maintenance/backup keys",
                v.get("tps").is_some()
                    && v.get("avg_tick_ms").is_some()
                    && v.get("maintenance_mode").is_some()
                    && v.get("backup_in_progress").is_some(),
            );
        }
        None => check("GET /status authed 200", false),
    }

    // -- log tail + join/leave parse (the UnboundLocalError regression) ----
    std::fs::OpenOptions::new()
        .append(true)
        .open(logs_dir.join("latest.log"))
        .and_then(|mut f| {
            use std::io::Write;
            f.write_all(b"[12:00:10] [Server thread/INFO]: Steve joined the game\n")
        })
        .unwrap();
    let mut saw_steve = false;
    for _ in 0..16 {
        std::thread::sleep(Duration::from_millis(500));
        if let Some(r) = get_a("/status") {
            if let Ok(v) = rt.block_on(async { r.json::<serde_json::Value>().await }) {
                if v["players"].as_array().map(|a| a.iter().any(|p| p == "Steve")).unwrap_or(false) {
                    saw_steve = true;
                    break;
                }
            }
        }
    }
    check("join line parsed → Steve tracked in /status", saw_steve);

    // /logs must contain the joined line — the log buffer actually fills now
    let logs = get_a("/logs")
        .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok());
    check(
        "GET /logs contains appended join line",
        logs.map(|v| {
            v["logs"]
                .as_array()
                .map(|a| a.iter().any(|l| l.as_str().unwrap_or("").contains("Steve joined the game")))
                .unwrap_or(false)
        })
        .unwrap_or(false),
    );

    // leave line removes the player
    std::fs::OpenOptions::new()
        .append(true)
        .open(logs_dir.join("latest.log"))
        .and_then(|mut f| {
            use std::io::Write;
            f.write_all(b"[12:00:20] [Server thread/INFO]: Steve left the game\n")
        })
        .unwrap();
    let mut steve_gone = false;
    for _ in 0..16 {
        std::thread::sleep(Duration::from_millis(500));
        if let Some(r) = get_a("/status") {
            if let Ok(v) = rt.block_on(async { r.json::<serde_json::Value>().await }) {
                if !v["players"].as_array().map(|a| a.iter().any(|p| p == "Steve")).unwrap_or(true) {
                    steve_gone = true;
                    break;
                }
            }
        }
    }
    check("leave line parsed → Steve removed", steve_gone);

    // -- settings: admin GET 200 + PUT rcon --------------------------------
    check(
        "admin GET /settings 200",
        get_a("/settings").map(|r| r.status().as_u16() == 200).unwrap_or(false),
    );
    check(
        "PUT /settings rcon_port 200",
        rt.block_on(
            http.put(format!("{base}/settings"))
                .header("Cookie", &admin_cookie)
                .json(&json!({"rcon_port": 25576}))
                .send(),
        )
        .map(|r| r.status().as_u16() == 200)
        .unwrap_or(false),
    );

    // -- maintenance gate ---------------------------------------------------
    check(
        "maintenance toggle on",
        post_a("/settings/maintenance/toggle", json!({}))
            .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
            .map(|v| v["maintenance_mode"] == json!(true))
            .unwrap_or(false),
    );
    // admin start during maintenance → gate passes to next check (run.bat missing)
    let start_msg = post_a("/power/start", json!({}))
        .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
        .and_then(|v| v["message"].as_str().map(String::from));
    check(
        "admin start passes maintenance gate",
        start_msg.map(|m| !m.contains("Maintenance")).unwrap_or(false),
    );
    check(
        "maintenance toggle back off",
        post_a("/settings/maintenance/toggle", json!({}))
            .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
            .map(|v| v["maintenance_mode"] == json!(false))
            .unwrap_or(false),
    );

    // -- emergency shutdown: admin ok, no java → graceful no-op -------------
    check(
        "admin emergency-shutdown 200",
        post_a("/emergency-shutdown", json!({}))
            .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
            .map(|v| v["success"] == json!(true))
            .unwrap_or(false),
    );

    // -- backups: create → list → restore → delete --------------------------
    let bid = post_a("/backups/create", json!({}))
        .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
        .and_then(|v| {
            check("backup create success", v["success"] == json!(true));
            v["backup_id"].as_str().map(String::from)
        });
    if let Some(bid) = bid {
        check(
            "backup has both dimension dirs",
            srv.join("backups").join(&bid).join("world").is_dir()
                && srv.join("backups").join(&bid).join("world_nether").is_dir(),
        );
        check(
            "backup in list",
            get_a("/backups")
                .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
                .map(|v| {
                    v["backups"]
                        .as_array()
                        .map(|a| a.iter().any(|b| b["id"] == bid))
                        .unwrap_or(false)
                })
                .unwrap_or(false),
        );
        // restore: destroy world file, restore, check it came back
        let target = srv.join("world").join("level.dat");
        let _ = std::fs::remove_file(&target);
        check(
            "backup restore (IDLE) ok",
            post_a(&format!("/backups/restore/{bid}"), json!({}))
                .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
                .map(|v| v["success"] == json!(true))
                .unwrap_or(false),
        );
        check("restored file exists", target.is_file());
        check(
            "backup delete ok",
            del_a(&format!("/backups/{bid}"))
                .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
                .map(|v| v["success"] == json!(true))
                .unwrap_or(false),
        );
    }

    // -- viewer role: extended viewing + start/stop, no powerful commands ---
    let viewer_cookie = post_a(
        "/users",
        json!({"username": "sandboxview", "password": "SandboxPass1", "role": "viewer"}),
    )
    .and_then(|r| {
        check("admin creates viewer user", r.status().as_u16() == 200);
        rt.block_on(
            http.post(format!("{base}/auth/login"))
                .json(&json!({"username": "sandboxview", "password": "SandboxPass1"}))
                .send(),
        )
        .ok()
        .and_then(|r| {
            r.headers()
                .get("set-cookie")
                .and_then(|v| v.to_str().ok())
                .map(|s| s.split(';').next().unwrap_or("").to_string())
        })
    });
    if let Some(vc) = viewer_cookie.filter(|c| c.contains("access_token=")) {
        let get_v = |p: &str| {
            rt.block_on(
                http.get(format!("{base}{p}"))
                    .header("Cookie", &vc)
                    .send(),
            )
            .ok()
        };
        let post_v = |p: &str| {
            rt.block_on(
                http.post(format!("{base}{p}"))
                    .header("Cookie", &vc)
                    .json(&json!({}))
                    .send(),
            )
            .ok()
        };
        check(
            "viewer GET /status 200 (extended viewing)",
            get_v("/status").map(|r| r.status().as_u16() == 200).unwrap_or(false),
        );
        check(
            "viewer GET /logs 200",
            get_v("/logs").map(|r| r.status().as_u16() == 200).unwrap_or(false),
        );
        check(
            "viewer GET /players/stats 200",
            get_v("/players/stats").map(|r| r.status().as_u16() == 200).unwrap_or(false),
        );
        check(
            "viewer /power/start allowed (reaches server gate)",
            post_v("/power/start")
                .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
                .map(|v| !v["message"].as_str().unwrap_or("").contains("ermission"))
                .unwrap_or(false),
        );
        check(
            "viewer console command → 403",
            rt.block_on(
                http.post(format!("{base}/console/command"))
                    .header("Cookie", &vc)
                    .json(&json!({"command": "say hi"}))
                    .send(),
            )
            .map(|r| r.status().as_u16() == 403)
            .unwrap_or(false),
        );
        check(
            "viewer GET /settings → 403",
            get_v("/settings").map(|r| r.status().as_u16() == 403).unwrap_or(false),
        );
        check(
            "viewer emergency-shutdown → 403",
            post_v("/emergency-shutdown")
                .map(|r| r.status().as_u16() == 403)
                .unwrap_or(false),
        );
        check(
            "viewer maintenance toggle → 403",
            post_v("/settings/maintenance/toggle")
                .map(|r| r.status().as_u16() == 403)
                .unwrap_or(false),
        );
    } else {
        check("viewer login set access_token cookie", false);
    }

    // == wave 2: remaining endpoints ========================================

    // static + health-ish
    check(
        "GET / dashboard 200",
        get_a("/").map(|r| r.status().as_u16() == 200).unwrap_or(false),
    );
    check(
        "GET /test 200",
        get_a("/test").map(|r| r.status().as_u16() == 200).unwrap_or(false),
    );

    // authed identity endpoints
    check(
        "GET /auth/me 200 admin",
        get_a("/auth/me")
            .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
            .map(|v| v["username"] == json!("admin"))
            .unwrap_or(false),
    );
    check(
        "GET /auth/verify 200",
        get_a("/auth/verify").map(|r| r.status().as_u16() == 200).unwrap_or(false),
    );

    // user administration
    let users = get_a("/users")
        .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok());
    let viewer_id = users.as_ref().and_then(|v| {
        v.as_array().and_then(|a| {
            a.iter()
                .find(|u| u["username"] == "sandboxview")
                .and_then(|u| u["id"].as_str())
                .map(String::from)
        })
    });
    check("admin GET /users lists sandboxview", viewer_id.is_some());
    if let Some(vid) = viewer_id {
        check(
            "admin PUT role → moderator",
            rt.block_on(
                http.put(format!("{base}/users/{vid}/role?role=moderator"))
                    .header("Cookie", &admin_cookie)
                    .send(),
            )
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
        );
        check(
            "admin reset viewer password",
            rt.block_on(
                http.put(format!("{base}/users/{vid}/password"))
                    .header("Cookie", &admin_cookie)
                    .json(&json!({"new_password": "ResetPass9"}))
                    .send(),
            )
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
        );
        check(
            "viewer login with reset password",
            rt.block_on(
                http.post(format!("{base}/auth/login"))
                    .json(&json!({"username": "sandboxview", "password": "ResetPass9"}))
                    .send(),
            )
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
        );
    }

    // second throwaway user for delete/status tests
    let victim_id = post_a(
        "/users",
        json!({"username": "victimuser", "password": "VictimPass1", "role": "viewer"}),
    )
    .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
    .and_then(|v| v["user"]["id"].as_str().map(String::from));
    check("admin creates victim user", victim_id.is_some());
    if let Some(id) = &victim_id {
        check(
            "admin deactivate user",
            rt.block_on(
                http.put(format!("{base}/users/{id}/status?is_active=false"))
                    .header("Cookie", &admin_cookie)
                    .send(),
            )
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
        );
        let dead_login = rt
            .block_on(
                http.post(format!("{base}/auth/login"))
                    .json(&json!({"username": "victimuser", "password": "VictimPass1"}))
                    .send(),
            )
            .ok();
        check(
            "deactivated user login rejected",
            dead_login.map(|r| r.status().as_u16() != 200).unwrap_or(false),
        );
        check(
            "admin DELETE user",
            del_a(&format!("/users/{id}"))
                .map(|r| r.status().as_u16() == 200)
                .unwrap_or(false),
        );
    }

    // player data + management
    check(
        "GET /players/Steve/data 200 (stats persisted)",
        get_a("/players/Steve/data")
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
    );
    check(
        "admin gamemode endpoint reachable",
        post_a("/players/Steve/gamemode?gamemode=creative", json!({}))
            .map(|r| r.status().as_u16() != 403)
            .unwrap_or(false),
    );
    check(
        "DELETE /players/Steve admin ok",
        del_a("/players/Steve")
            .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
            .map(|v| v["success"] == json!(true))
            .unwrap_or(false),
    );

    // console + power
    check(
        "GET /console/status 200",
        get_a("/console/status").map(|r| r.status().as_u16() == 200).unwrap_or(false),
    );
    check(
        "admin console command 200",
        post_a("/console/command", json!({"command": "say sandbox"}))
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
    );
    check(
        "admin /power/stop reaches gate",
        post_a("/power/stop", json!({}))
            .map(|r| r.status().as_u16() != 403)
            .unwrap_or(false),
    );
    check(
        "admin /power/mode 200",
        post_a("/power/mode", json!({"mode": "power_saver"}))
            .map(|r| r.status().as_u16() != 403)
            .unwrap_or(false),
    );
    check(
        "GET /resources/history 200",
        get_a("/resources/history").map(|r| r.status().as_u16() == 200).unwrap_or(false),
    );
    check(
        "GET /backups/settings 200",
        get_a("/backups/settings").map(|r| r.status().as_u16() == 200).unwrap_or(false),
    );
    check(
        "PUT /backups/settings 200",
        rt.block_on(
            http.put(format!("{base}/backups/settings"))
                .header("Cookie", &admin_cookie)
                .json(&json!({"backup_auto_delete_enabled": true}))
                .send(),
        )
        .map(|r| r.status().as_u16() == 200)
        .unwrap_or(false),
    );

    // register → auto-login → viewer role
    let reg = rt
        .block_on(
            http.post(format!("{base}/auth/register"))
                .json(&json!({"username": "reguser", "password": "RegPassw0rd"}))
                .send(),
        )
        .ok();
    match reg {
        Some(r) => {
            check("POST /auth/register 200", r.status().as_u16() == 200);
            let rc = r
                .headers()
                .get("set-cookie")
                .and_then(|v| v.to_str().ok())
                .map(|s| s.split(';').next().unwrap_or("").to_string());
            check(
                "registered user is viewer + authed",
                rc.filter(|c| c.contains("access_token="))
                    .and_then(|c| {
                        rt.block_on(
                            http.get(format!("{base}/auth/me"))
                                .header("Cookie", c)
                                .send(),
                        )
                        .ok()
                    })
                    .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
                    .map(|v| v["role"] == json!("viewer"))
                    .unwrap_or(false),
            );
        }
        None => check("POST /auth/register 200", false),
    }

    // change-password on the sandboxview account (require_password_change user)
    let vc2 = rt
        .block_on(
            http.post(format!("{base}/auth/login"))
                .json(&json!({"username": "sandboxview", "password": "ResetPass9"}))
                .send(),
        )
        .ok()
        .and_then(|r| {
            r.headers()
                .get("set-cookie")
                .and_then(|v| v.to_str().ok())
                .map(|s| s.split(';').next().unwrap_or("").to_string())
        });
    if let Some(vc2) = vc2 {
        check(
            "POST /auth/change-password ok",
            rt.block_on(
                http.post(format!("{base}/auth/change-password"))
                    .header("Cookie", &vc2)
                    .json(&json!({"new_password": "ChangedPass2"}))
                    .send(),
            )
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
        );
        check(
            "login with changed password",
            rt.block_on(
                http.post(format!("{base}/auth/login"))
                    .json(&json!({"username": "sandboxview", "password": "ChangedPass2"}))
                    .send(),
            )
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
        );
        check(
            "old password rejected",
            rt.block_on(
                http.post(format!("{base}/auth/login"))
                    .json(&json!({"username": "sandboxview", "password": "ResetPass9"}))
                    .send(),
            )
            .map(|r| r.status().as_u16() != 200)
            .unwrap_or(false),
        );

        // maintenance IP whitelist — real check, not the dead old list
        let post_v2 = |p: &str| {
            rt.block_on(
                http.post(format!("{base}{p}"))
                    .header("Cookie", &vc2)
                    .json(&json!({}))
                    .send(),
            )
            .ok()
            .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok())
        };
        post_a("/settings/maintenance/ips", json!({"ips": []}));
        post_a("/settings/maintenance/toggle", json!({}));
        check(
            "maintenance on + no whitelist → viewer start blocked",
            post_v2("/power/start")
                .map(|v| {
                    v["message"].as_str().unwrap_or("").contains("Maintenance")
                        || v["success"] == json!(false)
                })
                .unwrap_or(false),
        );
        post_a("/settings/maintenance/ips", json!({"ips": ["127.0.0.1"]}));
        check(
            "whitelisted 127.0.0.1 → viewer start passes maintenance",
            post_v2("/power/start")
                .map(|v| !v["message"].as_str().unwrap_or("").contains("Maintenance"))
                .unwrap_or(false),
        );
        post_a("/settings/maintenance/toggle", json!({})); // back off
        post_a("/settings/maintenance/ips", json!({"ips": []})); // cleanup
    } else {
        check("viewer relogin for wave-2", false);
    }

    // chat-text injection must NOT register a fake join
    std::fs::OpenOptions::new()
        .append(true)
        .open(logs_dir.join("latest.log"))
        .and_then(|mut f| {
            use std::io::Write;
            f.write_all(b"[12:01:00] [Server thread/INFO]: <Steve> Bob joined the game\n[12:01:01] [Server thread/INFO]: Alex joined the game in a chat message\n")
        })
        .unwrap();
    std::thread::sleep(Duration::from_secs(3));
    let pl = get_a("/status")
        .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok());
    check(
        "chat injection does not create fake players",
        pl.map(|v| {
            let empty = Vec::new();
            let a = v["players"].as_array().unwrap_or(&empty);
            !a.iter().any(|p| p == "Bob" || p == "Alex")
        })
        .unwrap_or(false),
    );

    // log flood → buffer stays bounded at LOG_BUFFER_SIZE
    {
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(logs_dir.join("latest.log"))
            .unwrap();
        for i in 0..560 {
            writeln!(f, "[12:0{}:{:02}] [Server thread/INFO]: flood {}", i / 60 % 10, i % 60, i).unwrap();
        }
    }
    std::thread::sleep(Duration::from_secs(3));
    let lg = get_a("/logs")
        .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok());
    check(
        "log buffer bounded (<=500) after flood",
        lg.map(|v| {
            v["logs"].as_array().map(|a| a.len() <= 500 && !a.is_empty()).unwrap_or(false)
        })
        .unwrap_or(false),
    );

    // unauthed write-path sweep
    for (method, path) in [
        ("POST", "/power/stop"),
        ("POST", "/console/command"),
        ("POST", "/emergency-shutdown"),
        ("POST", "/backups/create"),
        ("POST", "/settings/maintenance/toggle"),
    ] {
        check(
            &format!("unauthed {method} {path} → 401"),
            rt.block_on(http.post(format!("{base}{path}")).json(&json!({})).send())
                .map(|r| r.status().as_u16() == 401)
                .unwrap_or(false),
        );
    }

    // logout + static asset mount
    check(
        "POST /auth/logout 200",
        post_a("/auth/logout", json!({}))
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
    );
    check(
        "GET /items icon 200 (static mount)",
        get_a("/items/acacia_boat.png")
            .map(|r| r.status().as_u16() == 200)
            .unwrap_or(false),
    );
    // reguser stays a real viewer (sandboxview was promoted to moderator above)
    if let Some(vc3) = rt
        .block_on(
            http.post(format!("{base}/auth/login"))
                .json(&json!({"username": "reguser", "password": "RegPassw0rd"}))
                .send(),
        )
        .ok()
        .and_then(|r| {
            r.headers()
                .get("set-cookie")
                .and_then(|v| v.to_str().ok())
                .map(|s| s.split(';').next().unwrap_or("").to_string())
        })
    {
        check(
            "viewer GET /ecohost-logs → 403",
            rt.block_on(
                http.get(format!("{base}/ecohost-logs"))
                    .header("Cookie", &vc3)
                    .send(),
            )
            .map(|r| r.status().as_u16() == 403)
            .unwrap_or(false),
        );
        check(
            "viewer GET /users → 403",
            rt.block_on(
                http.get(format!("{base}/users"))
                    .header("Cookie", &vc3)
                    .send(),
            )
            .map(|r| r.status().as_u16() == 403)
            .unwrap_or(false),
        );
        check(
            "viewer /power/stop allowed",
            rt.block_on(
                http.post(format!("{base}/power/stop"))
                    .header("Cookie", &vc3)
                    .json(&json!({}))
                    .send(),
            )
            .map(|r| r.status().as_u16() != 403)
            .unwrap_or(false),
        );
    }

    // ws requires auth — plain GET must not upgrade happily
    check(
        "GET /ws unauthed rejected",
        rt.block_on(http.get(format!("{base}/ws")).send())
            .map(|r| r.status().as_u16() != 200)
            .unwrap_or(false),
    );

    // login lockout LAST — it's per-IP and poisons subsequent logins
    let mut last_code = 0u16;
    for _ in 0..7 {
        if let Ok(r) = rt.block_on(
            http.post(format!("{base}/auth/login"))
                .json(&json!({"username": "admin", "password": "wrongpass"}))
                .send(),
        ) {
            last_code = r.status().as_u16();
        }
    }
    check("5+ bad logins → 429 lockout", last_code == 429);
    check(
        "correct password still locked out",
        rt.block_on(
            http.post(format!("{base}/auth/login"))
                .json(&json!({"username": "admin", "password": "admin"}))
                .send(),
        )
        .map(|r| r.status().as_u16() == 429)
        .unwrap_or(false),
    );

    // -- unauthed backup endpoint is still gated ----------------------------
    check(
        "GET /backups unauthed 401",
        rt.block_on(http.get(format!("{base}/backups")).send())
            .map(|r| r.status().as_u16() == 401)
            .unwrap_or(false),
    );

    // -- ping listener armed (log evidence; port may be busy on dev box) ----
    std::thread::sleep(Duration::from_secs(6));
    let eh = get_a("/ecohost-logs")
        .and_then(|r| rt.block_on(async { r.json::<serde_json::Value>().await }).ok());
    check(
        "ping listener armed or port-busy-logged",
        eh.map(|v| {
            v["logs"]
                .as_array()
                .map(|a| {
                    a.iter().any(|l| {
                        l.as_str().unwrap_or("").contains("PingListener")
                    })
                })
                .unwrap_or(false)
        })
        .unwrap_or(false),
    );
}

fn test_backend_sidecar(rt: &tokio::runtime::Runtime) {
    // The bundled exe proves PyInstaller data paths + frozen DATA_DIR work.
    let exe = repo_root()
        .join("src-tauri/binaries/ecohost-backend-x86_64-pc-windows-msvc.exe");
    if !exe.is_file() {
        println!("  SKIP  sidecar exe not built");
        return;
    }
    let mut cmd = Command::new(&exe);
    let Some(child) = spawn_headless(&mut cmd) else {
        check("sidecar exe spawned", false);
        return;
    };
    check("sidecar exe spawned", true);
    let _g = KillGuard(Some(child));
    test_backend_endpoints("sidecar", rt);
}

// ---------------------------------------------------------------- updater

/// Minimal file server for the fake release — the sandbox's stand-in for
/// GitHub Releases. Serves raw bytes; Range is honored when asked.
fn serve_dir(dir: PathBuf, port: u16) -> std::thread::JoinHandle<()> {
    let server = tiny_http::Server::http(format!("127.0.0.1:{port}")).unwrap();
    std::thread::spawn(move || loop {
        let Ok(Some(req)) = server.recv_timeout(Duration::from_secs(60)) else {
            continue;
        };
        let path = dir.join(req.url().trim_start_matches('/').split('?').next().unwrap_or(""));
        let body = std::fs::read(&path).unwrap_or_default();
        if body.is_empty() {
            let _ = req.respond(tiny_http::Response::from_string("{}").with_status_code(404));
            continue;
        }
        let range_from = req
            .headers()
            .iter()
            .find(|h| h.field.equiv("Range"))
            .and_then(|h| h.value.as_str().strip_prefix("bytes="))
            .and_then(|s| s.split('-').next()?.parse::<u64>().ok())
            .unwrap_or(0);
        let total = body.len() as u64;
        let slice = &body[(range_from.min(total)) as usize..];
        let mut resp = tiny_http::Response::from_data(slice.to_vec());
        if range_from > 0 {
            resp = resp.with_status_code(206);
        }
        let _ = req.respond(resp);
    })
}

fn write_fake_release(dir: &Path, version: &str) -> (PathBuf, String, String) {
    std::fs::create_dir_all(dir).unwrap();
    // Payload stands in for the NSIS installer — contents don't matter,
    // sha256/size in the signed manifest is what binds it.
    let payload = dir.join(format!("EcoHost_{version}_x64-setup.exe"));
    std::fs::write(&payload, vec![0xABu8; 128 * 1024]).unwrap();
    let mut h = sha2::Sha256::new();
    h.update(std::fs::read(&payload).unwrap());
    let sha = hex::encode(h.finalize());

    let man = json!({
        "app": "com.ecohost.app",
        "version": version,
        "channel": "stable",
        "arch": std::env::consts::ARCH,
        "notes": "sandbox release",
        "pubDate": "2026-01-01T00:00:00Z",
        "file": payload.file_name().unwrap().to_string_lossy(),
        "sha256": sha,
        "size": 128 * 1024u64,
    });
    let man_bytes = man.to_string();
    std::fs::write(dir.join("latest.json"), &man_bytes).unwrap();
    (payload, man_bytes, sha)
}

fn test_updater(rt: &tokio::runtime::Runtime, dir: &Path) {
    println!("[updater] end-to-end via local HTTP");
    let upd_dir = dir.join("updapp");
    std::fs::create_dir_all(&upd_dir).unwrap();
    updater::init(&upd_dir);

    // Throwaway signing key + signed manifest.
    let kp = minisign::KeyPair::generate_unencrypted_keypair().unwrap();
    let pubkey = kp.pk.to_base64();
    let rel_dir = dir.join("release");
    let (_payload, man_bytes, _sha) = write_fake_release(&rel_dir, "9.9.9");
    let sig = minisign::sign(
        Some(&kp.pk),
        &kp.sk,
        std::io::Cursor::new(man_bytes.clone()),
        None,
        None,
    )
    .unwrap();
    std::fs::write(rel_dir.join("latest.json.minisig"), sig.to_string()).unwrap();

    // Serve it + point the updater at it.
    let _srv = serve_dir(rel_dir.clone(), 48230);
    std::thread::sleep(Duration::from_millis(300));
    updater::set_release_base("http://127.0.0.1:48230");

    let http = reqwest::Client::new();

    // status → offer
    let st = rt.block_on(updater::app_update_status_pk(&http, &upd_dir, &pubkey));
    check("status offers v9.9.9", st.state == "offer" && st.version == "9.9.9");

    // download → downloaded
    let r = rt.block_on(updater::download_app_update_pk(
        &http,
        &upd_dir,
        |done, total| {
            let _ = (done, total);
        },
        &pubkey,
    ));
    check("download completes", r.is_ok());
    let st = rt.block_on(updater::app_update_status_pk(&http, &upd_dir, &pubkey));
    check(
        "status reports downloaded v9.9.9",
        st.state == "downloaded" && st.version == "9.9.9",
    );

    // staged install + script generation (script is NEVER spawned).
    let inst = updater::prepare_install(&upd_dir);
    check("prepare_install ok", inst.is_ok());
    let prev_exists = upd_dir.join("updates-dl/prev").is_dir();
    check("prev exe backup staged", prev_exists);
    if let Ok(p) = &inst {
        let script = updater::write_update_cmd(&upd_dir, p);
        check("write_update_cmd ok", script.is_ok());
        if let Ok(sp) = script {
            let body = std::fs::read_to_string(&sp).unwrap_or_default();
            check(
                "update.cmd waits for exe unlock, runs /S, restores on timeout, relaunches",
                body.contains(":waitexit")
                    && body.contains("/S")
                    && body.contains(":restore")
                    && body.contains(":launch")
                    && body.contains("PREV="),
            );
        }
    }

    // skip/unskip flow
    updater::skip_version(&upd_dir, "9.9.9");
    let st = rt.block_on(updater::app_update_status_pk(&http, &upd_dir, &pubkey));
    check(
        "skipped version surfaces as skipped",
        st.state == "skipped" || st.state == "downloaded",
    );
    updater::unskip_version(&upd_dir);

    // Forged manifest → rejected (error, no offer).
    let kp2 = minisign::KeyPair::generate_unencrypted_keypair().unwrap();
    let sig2 = minisign::sign(
        Some(&kp2.pk),
        &kp2.sk,
        std::io::Cursor::new(man_bytes),
        None,
        None,
    )
    .unwrap();
    let forge = dir.join("forge");
    std::fs::create_dir_all(&forge).unwrap();
    std::fs::write(forge.join("latest.json"), std::fs::read(rel_dir.join("latest.json")).unwrap())
        .unwrap();
    std::fs::write(forge.join("latest.json.minisig"), sig2.to_string()).unwrap();
    std::fs::copy(rel_dir.join("EcoHost_9.9.9_x64-setup.exe"), forge.join("EcoHost_9.9.9_x64-setup.exe"))
        .unwrap();
    let _srv2 = serve_dir(forge, 48231);
    std::thread::sleep(Duration::from_millis(200));
    updater::set_release_base("http://127.0.0.1:48231");
    let r = rt.block_on(updater::download_app_update_pk(
        &http,
        &dir.join("updapp2"),
        |_, _| {},
        &pubkey,
    ));
    check_err("forged signature rejected", &r, "verify failed");
}

// ---------------------------------------------------------------- main

fn main() {
    println!("=== ecohost sandbox ===");
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .unwrap();
    let t0 = Instant::now();
    let root = tmp_root();

    test_gate();
    test_sig();
    test_config(&root.join("tool"));

    // If a backend is already running on :8000 we test against it directly;
    // otherwise python source first, then the bundled sidecar exe.
    let already_up = backend::health_ok();
    if already_up {
        println!("[backend] existing backend on :8000 — testing against it");
        test_backend_endpoints("existing", &rt);
    } else {
        test_backend_python(&rt);
        std::thread::sleep(Duration::from_millis(800));
        test_backend_full(&rt);
        std::thread::sleep(Duration::from_millis(800));
        test_backend_sidecar(&rt);
    }

    test_updater(&rt, &root.join("upd"));

    // process::exit skips destructors — clean explicitly.
    let _ = std::fs::remove_dir_all(&root);
    let (p, f) = (PASS.load(Ordering::Relaxed), FAIL.load(Ordering::Relaxed));
    println!("\n=== sandbox done in {:?}: {} pass, {} failure(s) ===", t0.elapsed(), p, f);
    std::process::exit(if f == 0 { 0 } else { 1 });
}
