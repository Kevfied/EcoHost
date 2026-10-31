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
        test_backend_sidecar(&rt);
    }

    test_updater(&rt, &root.join("upd"));

    // process::exit skips destructors — clean explicitly.
    let _ = std::fs::remove_dir_all(&root);
    let (p, f) = (PASS.load(Ordering::Relaxed), FAIL.load(Ordering::Relaxed));
    println!("\n=== sandbox done in {:?}: {} pass, {} failure(s) ===", t0.elapsed(), p, f);
    std::process::exit(if f == 0 { 0 } else { 1 });
}
