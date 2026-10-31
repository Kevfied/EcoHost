//! Backend supervisor — owns the ecohost-backend sidecar (or a dev
//! `python main.py` fallback) lifecycle: spawn, health, restart, kill.
//! A backend already listening on :8000 is reused and left running.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::Manager;

use crate::config;

pub const BACKEND_PORT: u16 = 8000;
const HEALTH_TIMEOUT: Duration = Duration::from_secs(45);
const HEALTH_POLL: Duration = Duration::from_millis(250);

enum BackendProc {
    Std(Child),
    Sidecar(tauri_plugin_shell::process::CommandChild),
}

impl BackendProc {
    fn kill(self) {
        // A PyInstaller onefile exe re-execs itself — killing only the
        // bootloader parent orphans the real backend. Kill the whole tree.
        #[cfg(windows)]
        {
            let pid = match &self {
                BackendProc::Std(c) => c.id(),
                BackendProc::Sidecar(c) => c.pid(),
            };
            let mut cmd = Command::new("taskkill");
            cmd.args(["/T", "/F", "/PID", &pid.to_string()])
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
            let _ = cmd.status();
        }
        match self {
            BackendProc::Std(mut c) => {
                let _ = c.kill();
                let _ = c.wait();
            }
            BackendProc::Sidecar(c) => {
                let _ = c.kill();
            }
        }
    }
}

pub struct BackendState {
    /// The child we spawned (None when reusing an existing backend).
    proc: Mutex<Option<BackendProc>>,
    /// Server dir the running backend was spawned with — restart is
    /// required for a changed server_dir to take effect.
    spawned_with_dir: Mutex<Option<String>>,
}

impl BackendState {
    pub fn new() -> Self {
        Self {
            proc: Mutex::new(None),
            spawned_with_dir: Mutex::new(None),
        }
    }

    /// True when this app spawned the backend (vs. reusing a running one).
    pub fn is_managed(&self) -> bool {
        self.spawned_with_dir.lock().unwrap().is_some()
    }
}

pub fn health_ok() -> bool {
    let Ok(mut s) = TcpStream::connect(("127.0.0.1", BACKEND_PORT)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_millis(800)));
    let _ = s.set_write_timeout(Some(Duration::from_millis(800)));
    if s.write_all(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut buf = [0u8; 64];
    match s.read(&mut buf) {
        Ok(n) => {
            let head = String::from_utf8_lossy(&buf[..n]);
            head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200")
        }
        Err(_) => false,
    }
}

pub fn wait_healthy(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if health_ok() {
            return true;
        }
        std::thread::sleep(HEALTH_POLL);
    }
    health_ok()
}

/// Project root in dev: walk ancestors of the exe until a dir containing
/// main.py shows up (works from target/debug/ and target/debug/examples/).
fn dev_project_root() -> Option<PathBuf> {
    for anc in std::env::current_exe().ok()?.ancestors() {
        if anc.join("main.py").exists() {
            return Some(anc.to_path_buf());
        }
    }
    None
}

/// Where backend stdout/stderr goes — the sidecar is a --noconsole exe,
/// so this is the only place its logs survive.
pub fn backend_log_path() -> PathBuf {
    crate::config::app_dir()
        .join("logs")
        .join("backend.log")
}

fn append_log(line: &[u8]) {
    use std::io::Write;
    let p = backend_log_path();
    if let Some(dir) = p.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&p) {
        let _ = f.write_all(line);
        let _ = f.write_all(b"\n");
    }
}

fn spawn_sidecar(app: &tauri::AppHandle, server_dir: &str) -> Option<BackendProc> {
    use tauri_plugin_shell::ShellExt;
    let mut cmd = app.shell().sidecar("ecohost-backend").ok()?;
    if !server_dir.is_empty() {
        cmd = cmd.env("ECOHOST_SERVER_DIR", server_dir);
    }
    let (rx, child) = cmd.spawn().ok()?;
    // Drain stdout/stderr into backend.log — an unread pipe can deadlock
    // a chatty process.
    tauri::async_runtime::spawn(async move {
        use tauri_plugin_shell::process::CommandEvent;
        let mut rx = rx;
        while let Some(ev) = rx.recv().await {
            match ev {
                CommandEvent::Stdout(l) | CommandEvent::Stderr(l) => append_log(&l),
                _ => {}
            }
        }
    });
    Some(BackendProc::Sidecar(child))
}

fn spawn_python_dev(server_dir: &str) -> Option<BackendProc> {
    let root = dev_project_root()?;
    let mut cmd = Command::new("python");
    cmd.arg("main.py")
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if !server_dir.is_empty() {
        cmd.env("ECOHOST_SERVER_DIR", server_dir);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = cmd.spawn().ok()?;
    use std::io::{BufRead, BufReader};
    if let Some(s) = child.stdout.take() {
        std::thread::spawn(move || {
            for line in BufReader::new(s).lines().map_while(Result::ok) {
                append_log(line.as_bytes());
            }
        });
    }
    if let Some(s) = child.stderr.take() {
        std::thread::spawn(move || {
            for line in BufReader::new(s).lines().map_while(Result::ok) {
                append_log(line.as_bytes());
            }
        });
    }
    Some(BackendProc::Std(child))
}

/// Ensure a backend is up: reuse :8000 if it already answers, else spawn
/// sidecar → python fallback. Waits for health. Returns what happened.
pub fn ensure_running(app: &tauri::AppHandle) -> &'static str {
    if health_ok() {
        return "reused";
    }
    let server_dir = config::load().server_dir;
    if let Some(proc) =
        spawn_sidecar(app, &server_dir).or_else(|| spawn_python_dev(&server_dir))
    {
        app.state::<BackendState>().proc.lock().unwrap().replace(proc);
        *app.state::<BackendState>()
            .spawned_with_dir
            .lock()
            .unwrap() = Some(server_dir);
    }
    if wait_healthy(HEALTH_TIMEOUT) {
        "spawned"
    } else {
        "failed"
    }
}

/// Kill our backend child if it exists. A reused backend is left alone.
pub fn kill(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<BackendState>() {
        if let Some(proc) = state.proc.lock().unwrap().take() {
            proc.kill();
        }
        state.spawned_with_dir.lock().unwrap().take();
    }
}

/// Kill ours (if any), wait for the port to go quiet, then ensure again.
pub fn restart(app: &tauri::AppHandle) -> &'static str {
    kill(app);
    let deadline = Instant::now() + Duration::from_secs(15);
    while health_ok() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(200));
    }
    ensure_running(app)
}

/// Which server_dir our spawned backend is using (None = reused/none).
pub fn spawned_dir(app: &tauri::AppHandle) -> Option<String> {
    app.try_state::<BackendState>()?
        .spawned_with_dir
        .lock()
        .unwrap()
        .clone()
}
