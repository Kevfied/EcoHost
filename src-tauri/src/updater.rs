//! App self-update — manifest + payload hosted as GitHub Release assets.
//! Ported from mc-modder's LAN updater: trust comes from a detached
//! minisign signature on the manifest, not from the host. The payload is
//! bound by sha256 inside the signed manifest.
//!
//! Strict rule: nothing installs without an explicit user action.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use sha2::Digest;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, SystemTime};

/// The developer's update public key — the ONLY trust root for updates.
/// Private half lives in %LOCALAPPDATA%\EcoHost\update-sign.key
/// (never committed). Regenerate + redistribute the app if it's ever lost.
pub const UPDATE_PUBKEY: &str = "RWRKDPUQ4s+ZejxLLPTUsrlG7KOlB/fAlreNWA40uTtqXB5eNZDjGdDh";
const APP_ID: &str = "com.ecohost.app";
const CHANNEL: &str = "stable";
const MANIFEST_CAP: usize = 64 * 1024;

/// Release asset base — GitHub resolves `latest` to the newest release.
const RELEASE_BASE: &str =
    "https://github.com/Kevfied/EcoHost/releases/latest/download";

/// Tests/tools redirect fetches to a local server.
static RELEASE_BASE_OVERRIDE: OnceLock<Mutex<String>> = OnceLock::new();

pub fn set_release_base(url: &str) {
    if let Some(m) = RELEASE_BASE_OVERRIDE.get() {
        *m.lock().unwrap() = url.to_string();
    } else {
        let _ = RELEASE_BASE_OVERRIDE.set(Mutex::new(url.to_string()));
    }
}

fn release_base() -> String {
    RELEASE_BASE_OVERRIDE
        .get()
        .map(|m| m.lock().unwrap().clone())
        .unwrap_or_else(|| RELEASE_BASE.to_string())
}

// ---------------------------------------------------------------- types

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateManifest {
    pub app: String,
    pub version: String,
    #[serde(default)]
    pub channel: String,
    #[serde(default)]
    pub arch: String,
    #[serde(default)]
    pub notes: String,
    #[serde(default, rename = "pubDate")]
    pub pub_date: String,
    pub file: String,
    pub sha256: String,
    pub size: u64,
}

/// What the UI renders. `state`: idle | offer | downloading | downloaded |
/// skipped.
#[derive(Debug, Clone, Serialize)]
pub struct AppUpdateStatus {
    pub state: String,
    pub version: String,
    pub notes: String,
    pub size: u64,
    pub progress: f64,
    #[serde(rename = "runningVersion")]
    pub running_version: String,
    /// Set while a "✓ updated" welcome is pending dismissal (the new version).
    #[serde(rename = "justUpdated")]
    pub just_updated: String,
    /// Release notes captured at install time for the justUpdated banner.
    #[serde(rename = "updatedNotes")]
    pub updated_notes: String,
    /// Last fetch error, if the update check itself failed.
    #[serde(default)]
    pub error: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct DownloadedEntry {
    pub version: String,
    pub path: String,
    pub sha256: String,
    #[serde(default)]
    pub notes: String,
}

#[derive(Debug, Serialize, Deserialize, Default)]
struct AppUpdateStore {
    #[serde(rename = "skippedVersion")]
    skipped_version: Option<String>,
    downloaded: Option<DownloadedEntry>,
    /// Version the app was running at last status check — detects upgrades.
    #[serde(default, rename = "lastRun")]
    last_run: Option<String>,
    /// New version awaiting the post-update banner's dismissal.
    #[serde(default, rename = "welcomePending")]
    welcome_pending: Option<String>,
    /// Notes for the version most recently installed via the updater.
    #[serde(default, rename = "installedNotes")]
    installed_notes: String,
}

#[derive(Default)]
struct DownloadState {
    running: bool,
    cancel: bool,
    done: u64,
    total: u64,
}

static APP_DIR: OnceLock<PathBuf> = OnceLock::new();
static DL: OnceLock<Mutex<DownloadState>> = OnceLock::new();
static LAST_ERROR: OnceLock<Mutex<String>> = OnceLock::new();

fn dl_state() -> &'static Mutex<DownloadState> {
    DL.get_or_init(|| Mutex::new(DownloadState::default()))
}

fn last_error() -> &'static Mutex<String> {
    LAST_ERROR.get_or_init(|| Mutex::new(String::new()))
}

fn set_error(e: &str) {
    *last_error().lock().unwrap() = e.to_string();
}

// ---------------------------------------------------------------- paths

/// Called once at app startup (and by tests) — anchors all update dirs.
pub fn init(app_dir: &Path) {
    let _ = APP_DIR.set(app_dir.to_path_buf());
}

#[allow(dead_code)]
fn app_dir() -> PathBuf {
    APP_DIR
        .get()
        .cloned()
        .unwrap_or_else(crate::config::app_dir)
}

pub fn dl_dir(app_dir: &Path) -> PathBuf {
    app_dir.join("updates-dl")
}

fn store_file(app_dir: &Path) -> PathBuf {
    app_dir.join("appupdate.json")
}

fn running_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

fn running_semver() -> semver::Version {
    semver::Version::parse(&running_version()).unwrap_or_else(|_| semver::Version::new(0, 0, 0))
}

// ---------------------------------------------------------------- store

fn load_store(app_dir: &Path) -> AppUpdateStore {
    std::fs::read_to_string(store_file(app_dir))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn save_store(app_dir: &Path, s: &AppUpdateStore) {
    if let Ok(j) = serde_json::to_string_pretty(s) {
        let f = store_file(app_dir);
        if let Some(p) = f.parent() {
            let _ = std::fs::create_dir_all(p);
        }
        // Atomic: readers and crashes must never see a half-written store.
        let tmp = f.with_extension("tmp");
        if std::fs::write(&tmp, j).is_ok() {
            let _ = std::fs::rename(&tmp, &f);
        }
    }
}

pub fn skip_version(app_dir: &Path, version: &str) {
    let mut s = load_store(app_dir);
    s.skipped_version = Some(version.to_string());
    save_store(app_dir, &s);
}

pub fn unskip_version(app_dir: &Path) {
    let mut s = load_store(app_dir);
    s.skipped_version = None;
    save_store(app_dir, &s);
}

// ---------------------------------------------------------------- manifest gate

fn bare_filename_ok(name: &str) -> bool {
    !name.is_empty()
        && name.len() < 256
        && !name.contains('/')
        && !name.contains('\\')
        && !name.contains(':')
        && !name.contains("..")
}

/// Everything checked before an offer is even shown.
pub fn gate_manifest(m: &UpdateManifest, current: &str, skipped: Option<&str>) -> Result<()> {
    if m.app != APP_ID {
        anyhow::bail!("manifest is for a different app");
    }
    if !m.channel.is_empty() && m.channel != CHANNEL {
        anyhow::bail!("wrong channel");
    }
    if !m.arch.is_empty() && m.arch != std::env::consts::ARCH {
        anyhow::bail!("wrong architecture");
    }
    let v = semver::Version::parse(&m.version).context("bad version in manifest")?;
    let cur = semver::Version::parse(current).unwrap_or_else(|_| semver::Version::new(0, 0, 0));
    if v <= cur {
        anyhow::bail!("not newer");
    }
    if Some(m.version.as_str()) == skipped {
        anyhow::bail!("version skipped");
    }
    if !bare_filename_ok(&m.file) {
        anyhow::bail!("bad filename in manifest");
    }
    Ok(())
}

/// Verify the detached minisign signature over the raw manifest bytes.
pub fn verify_manifest_sig(
    manifest_bytes: &[u8],
    sig_text: &str,
    pubkey_b64: &str,
) -> Result<()> {
    let pk = minisign_verify::PublicKey::from_base64(pubkey_b64)
        .map_err(|e| anyhow::anyhow!("bad pubkey: {e}"))?;
    let sig = minisign_verify::Signature::decode(sig_text)
        .map_err(|e| anyhow::anyhow!("bad signature: {e}"))?;
    pk.verify(manifest_bytes, &sig, false)
        .map_err(|e| anyhow::anyhow!("signature verify failed: {e}"))
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut f = std::fs::File::open(path)?;
    let mut h = sha2::Sha256::new();
    std::io::copy(&mut f, &mut h)?;
    Ok(hex::encode(h.finalize()))
}

// ---------------------------------------------------------------- fetch/verify

async fn fetch_capped(http: &reqwest::Client, url: &str) -> Result<Vec<u8>> {
    let mut r = http
        .get(url)
        .timeout(Duration::from_secs(10))
        .send()
        .await?
        .error_for_status()?;
    let mut out = Vec::new();
    while let Some(chunk) = r.chunk().await? {
        out.extend_from_slice(&chunk);
        if out.len() > MANIFEST_CAP {
            anyhow::bail!("response too large");
        }
    }
    Ok(out)
}

/// Fetch + sig-verify + gate the newest manifest from GitHub Releases.
async fn verified_manifest(
    http: &reqwest::Client,
    skipped: Option<&str>,
    pubkey: &str,
) -> Result<UpdateManifest> {
    let base = release_base();
    let bytes = fetch_capped(http, &format!("{base}/latest.json")).await?;
    let sig_bytes = fetch_capped(http, &format!("{base}/latest.json.minisig")).await?;
    let sig_text = String::from_utf8_lossy(&sig_bytes).into_owned();
    verify_manifest_sig(&bytes, &sig_text, pubkey)?;
    let m: UpdateManifest = serde_json::from_slice(&bytes).context("bad manifest json")?;
    gate_manifest(&m, &running_version(), skipped)?;
    Ok(m)
}

// ---------------------------------------------------------------- status

pub async fn app_update_status(http: &reqwest::Client, dir: &Path) -> AppUpdateStatus {
    app_update_status_pk(http, dir, UPDATE_PUBKEY).await
}

/// Same as `app_update_status` but with an injectable key (tests).
pub async fn app_update_status_pk(
    http: &reqwest::Client,
    dir: &Path,
    pubkey: &str,
) -> AppUpdateStatus {
    let running = running_version();
    let mut store = load_store(dir);
    // Version changed since last run → a welcome banner is owed. Flagged
    // persistently so it's emitted until the user dismisses it.
    if store.last_run.as_deref() != Some(running.as_str()) {
        if store.last_run.is_some() {
            store.welcome_pending = Some(running.clone());
        }
        store.last_run = Some(running.clone());
        save_store(dir, &store);
    }
    let just_updated = store.welcome_pending.clone().unwrap_or_default();
    let updated_notes = if just_updated.is_empty() {
        String::new()
    } else {
        store.installed_notes.clone()
    };
    let base = |state: &str| AppUpdateStatus {
        state: state.into(),
        version: String::new(),
        notes: String::new(),
        size: 0,
        progress: 0.0,
        running_version: running.clone(),
        just_updated: just_updated.clone(),
        updated_notes: updated_notes.clone(),
        error: last_error().lock().unwrap().clone(),
    };

    // Downloaded-and-verified bundle? Surfaces even while offline.
    let downloaded_ver = store.downloaded.as_ref().and_then(|d| {
        let p = PathBuf::from(&d.path);
        let ok = p.is_file()
            && semver::Version::parse(&d.version)
                .map(|v| v > running_semver())
                .unwrap_or(false)
            && sha256_file(&p).map(|h| h == d.sha256).unwrap_or(false);
        ok.then(|| (d.version.clone(), d.notes.clone()))
    });

    let (dl_running, dl_progress) = {
        let st = dl_state().lock().unwrap();
        (
            st.running,
            if st.total > 0 {
                st.done as f64 / st.total as f64
            } else {
                0.0
            },
        )
    };
    if dl_running {
        let mut s = base("downloading");
        s.progress = dl_progress;
        return s;
    }

    match verified_manifest(http, store.skipped_version.as_deref(), pubkey).await {
        Ok(m) => {
            set_error("");
            // A staged download of the same version — skip the offer.
            let same_as_dl = downloaded_ver
                .as_ref()
                .map(|(dv, _)| dv == &m.version)
                .unwrap_or(false);
            if !same_as_dl {
                let mut s = base("offer");
                s.version = m.version.clone();
                s.notes = m.notes;
                s.size = m.size;
                return s;
            }
        }
        Err(e) => {
            // "not newer"/"version skipped" are quiet non-errors.
            let msg = e.to_string();
            if msg != "not newer" && msg != "version skipped" {
                set_error(&msg);
            } else {
                set_error("");
            }
        }
    }

    if let Some((dv, dnotes)) = downloaded_ver {
        let mut s = base("downloaded");
        s.version = dv;
        s.notes = dnotes;
        s.progress = 1.0;
        return s;
    }

    let mut s = base("idle");
    if let Some(sv) = &store.skipped_version {
        let newer = semver::Version::parse(sv)
            .map(|v| v > running_semver())
            .unwrap_or(false);
        if newer {
            s.state = "skipped".into();
            s.version = sv.clone();
        }
    }
    s
}

// ---------------------------------------------------------------- download

pub fn cancel_download() {
    let mut st = dl_state().lock().unwrap();
    if st.running {
        st.cancel = true;
    }
}

/// Download the update payload: sig-verified manifest, Range resume,
/// sha256 gate, atomic rename. `progress(done,total)`.
pub async fn download_app_update(
    http: &reqwest::Client,
    dir: &Path,
    progress: impl Fn(u64, u64),
) -> Result<String> {
    download_app_update_pk(http, dir, progress, UPDATE_PUBKEY).await
}

/// Same as `download_app_update` but with an injectable key (tests).
pub async fn download_app_update_pk(
    http: &reqwest::Client,
    dir: &Path,
    progress: impl Fn(u64, u64),
    pubkey: &str,
) -> Result<String> {
    {
        let mut st = dl_state().lock().unwrap();
        if st.running {
            anyhow::bail!("download already in progress");
        }
        st.running = true;
        st.cancel = false;
        st.done = 0;
        st.total = 0;
    }
    let r = download_inner(http, dir, &progress, pubkey).await;
    let mut st = dl_state().lock().unwrap();
    st.running = false;
    r
}

async fn download_inner(
    http: &reqwest::Client,
    dir: &Path,
    progress: &impl Fn(u64, u64),
    pubkey: &str,
) -> Result<String> {
    let store = load_store(dir);
    let m = verified_manifest(http, store.skipped_version.as_deref(), pubkey).await?;

    let dl = dl_dir(dir);
    std::fs::create_dir_all(&dl)?;
    let final_path = dl.join(&m.file);
    let part_path = dl.join(format!("{}.part", m.file));

    // Free-space preflight.
    let free = fs2::free_space(&dl).unwrap_or(u64::MAX);
    if m.size > 0 && free < m.size {
        anyhow::bail!("not enough disk space");
    }

    // Resume from a .part if present.
    let mut have = part_path.metadata().map(|x| x.len()).unwrap_or(0);
    if have > m.size {
        let _ = std::fs::remove_file(&part_path);
        have = 0;
    }
    // Connect bounds only connect+stalls, never a total deadline. Dropped
    // connections retry-and-resume off the surviving .part.
    let url = format!("{}/{}", release_base(), m.file);
    let mut done;
    use std::io::Write;
    let mut attempt = 0usize;
    loop {
        attempt += 1;
        let mut req = http.get(&url);
        if have > 0 {
            req = req.header("Range", format!("bytes={have}-"));
        }
        let mut r = match tokio::time::timeout(Duration::from_secs(10), req.send()).await {
            Ok(Ok(r)) => r,
            Ok(Err(e)) => {
                if attempt >= 3 {
                    return Err(e.into());
                }
                tokio::time::sleep(Duration::from_millis(500)).await;
                continue;
            }
            Err(_) => {
                if attempt >= 3 {
                    anyhow::bail!("release host unreachable");
                }
                tokio::time::sleep(Duration::from_millis(500)).await;
                continue;
            }
        };
        let status = r.status().as_u16();
        if status == 416 {
            let _ = std::fs::remove_file(&part_path);
            anyhow::bail!("range rejected");
        }
        r.error_for_status_ref()?;
        let append = status == 206 && have > 0;
        if let Some(len) = r.content_length() {
            if !append && len != m.size {
                anyhow::bail!("size mismatch from release host");
            }
        }
        let mut out = std::fs::OpenOptions::new()
            .create(true)
            .write(true)
            .append(append)
            .truncate(!append)
            .open(&part_path)?;
        done = if append { have } else { 0 };
        {
            let mut st = dl_state().lock().unwrap();
            st.done = done;
            st.total = m.size;
        }
        // Per-chunk stall guard — no bytes for 30s means the link's dead.
        let mut stream_err: Option<anyhow::Error> = None;
        loop {
            match tokio::time::timeout(Duration::from_secs(30), r.chunk()).await {
                Err(_) => {
                    stream_err = Some(anyhow::anyhow!("stalled (no data for 30s)"));
                    break;
                }
                Ok(Err(e)) => {
                    stream_err = Some(e.into());
                    break;
                }
                Ok(Ok(None)) => break,
                Ok(Ok(Some(chunk))) => {
                    if dl_state().lock().unwrap().cancel {
                        stream_err = Some(anyhow::anyhow!("cancelled"));
                        break;
                    }
                    out.write_all(&chunk)?;
                    done += chunk.len() as u64;
                    dl_state().lock().unwrap().done = done;
                    progress(done, m.size);
                }
            }
        }
        drop(out);
        if stream_err.is_none() && done != m.size {
            stream_err = Some(anyhow::anyhow!("incomplete download"));
        }
        match stream_err {
            None => break,
            Some(e) => {
                if dl_state().lock().unwrap().cancel {
                    return Err(e);
                }
                have = part_path.metadata().map(|x| x.len()).unwrap_or(0);
                if attempt >= 3 {
                    return Err(e);
                }
                tokio::time::sleep(Duration::from_millis(500)).await;
            }
        }
    }
    if done != m.size {
        anyhow::bail!("incomplete download");
    }
    // Verify payload against the signed manifest.
    let h = sha256_file(&part_path)?;
    if h != m.sha256 {
        let _ = std::fs::remove_file(&part_path);
        anyhow::bail!("hash mismatch — payload rejected");
    }
    std::fs::rename(&part_path, &final_path)?;
    let mut s = load_store(dir);
    s.downloaded = Some(DownloadedEntry {
        version: m.version.clone(),
        path: final_path.to_string_lossy().into_owned(),
        sha256: m.sha256.clone(),
        notes: m.notes.clone(),
    });
    save_store(dir, &s);
    Ok(format!("✓ downloaded v{} — ready to install", m.version))
}

// ---------------------------------------------------------------- install

/// Re-verify the staged download and back up the current exe (escape hatch).
/// Returns the installer path — the caller decides when to spawn+exit.
pub fn prepare_install(dir: &Path) -> Result<PathBuf> {
    let s = load_store(dir);
    let d = s.downloaded.context("nothing downloaded")?;
    let p = PathBuf::from(&d.path);
    if !p.is_file() {
        anyhow::bail!("downloaded installer missing");
    }
    if sha256_file(&p)? != d.sha256 {
        anyhow::bail!("downloaded installer failed hash check");
    }
    // Escape hatch: keep the running exe so a broken release can't strand us.
    if let Ok(exe) = std::env::current_exe() {
        let prev = dl_dir(dir).join("prev");
        let _ = std::fs::create_dir_all(&prev);
        let name = exe
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| "EcoHost-prev.exe".into());
        let _ = std::fs::copy(&exe, prev.join(name));
    }
    Ok(p)
}

/// Write the detached install script for `installer`. Split from spawn so
/// tests can verify the script without ever launching it (running it would
/// relaunch *this* exe — a fork bomb inside the sandbox).
pub fn write_update_cmd(dir: &Path, installer: &Path) -> Result<PathBuf> {
    let script = dl_dir(dir).join("update.cmd");
    let exe = std::env::current_exe()
        .map(|e| e.display().to_string())
        .unwrap_or_default();
    let prev = dl_dir(dir)
        .join("prev")
        .join(
            std::env::current_exe()
                .ok()
                .and_then(|e| e.file_name().map(|n| n.to_string_lossy().into_owned()))
                .unwrap_or_else(|| "EcoHost-prev.exe".into()),
        )
        .display()
        .to_string();
    // Persist a trace of every install attempt — when the app won't come
    // back we need to know whether the script even ran, not just guess.
    let log = dir.join("logs").join("install.log").display().to_string();
    let body = format!(
        concat!(
            "@echo off\r\n",
            "setlocal EnableDelayedExpansion\r\n",
            "set \"APP={exe}\"\r\n",
            "set \"PREV={prev}\"\r\n",
            "set \"LOG={log}\"\r\n",
            "echo [%date% %time%] update.cmd start app=\"%APP%\" installer=\"{installer}\">> \"%LOG%\"\r\n",
            "set tries=0\r\n",
            ":waitexit\r\n",
            // timeout.exe needs a console — this runs CREATE_NO_WINDOW, so
            // it fails instantly; ping -n is the headless-safe ~1s sleep.
            "\"%SystemRoot%\\System32\\ping.exe\" -n 2 127.0.0.1 >nul\r\n",
            // Exe is writable only once the old process is truly gone.
            "2>nul ( >>\"%APP%\" (call ) ) && goto unlocked\r\n",
            "set /a tries+=1\r\n",
            "if !tries! lss 60 goto waitexit\r\n",
            ":unlocked\r\n",
            "echo [%date% %time%] old process released exe — running installer>> \"%LOG%\"\r\n",
            "\"{installer}\" /S\r\n",
            "echo [%date% %time%] installer parent returned>> \"%LOG%\"\r\n",
            "set tries=0\r\n",
            "set lasts=x\r\n",
            ":waitfile\r\n",
            "\"%SystemRoot%\\System32\\ping.exe\" -n 2 127.0.0.1 >nul\r\n",
            "set /a tries+=1\r\n",
            "if !tries! geq 120 goto restore\r\n",
            "if not exist \"%APP%\" goto waitfile\r\n",
            "for %%A in (\"%APP%\") do set cur=%%~zA\r\n",
            "if not \"!cur!\"==\"!lasts!\" (\r\n",
            "  set lasts=!cur!\r\n",
            "  goto waitfile\r\n",
            ")\r\n",
            "if \"!cur!\"==\"0\" goto waitfile\r\n",
            "if \"!cur!\"==\"\" goto waitfile\r\n",
            "goto launch\r\n",
            // Install never produced an exe — put the backup back so the
            // app still opens (old version) instead of leaving a dead icon.
            ":restore\r\n",
            "if not exist \"%APP%\" (\r\n",
            "  copy /y \"%PREV%\" \"%APP%\" >nul\r\n",
            "  echo [%date% %time%] install timed out — restored previous exe>> \"%LOG%\"\r\n",
            ")\r\n",
            ":launch\r\n",
            "echo [%date% %time%] launching \"%APP%\">> \"%LOG%\"\r\n",
            "start \"\" \"%APP%\"\r\n",
        ),
        exe = exe,
        prev = prev,
        log = log,
        installer = installer.display(),
    );
    std::fs::write(&script, body).context("failed to write update.cmd")?;
    Ok(script)
}

/// Prepare + spawn the installer detached. The caller exits the app after.
/// The installer runs SILENT (/S — no dialogs), then relaunches the updated
/// app once it finishes, so an update is a single click end-to-end.
pub fn install_app_update(dir: &Path) -> Result<String> {
    let p = prepare_install(dir)?;
    // Run via a .cmd — a naive "wait 2s, run installer, relaunch" strands
    // people: WebView2 can outlive the window (installer hits a locked exe)
    // and NSIS forks a helper child that keeps copying after its parent
    // returns, so relaunching immediately can start a half-written exe.
    let script = write_update_cmd(dir, &p)?;
    let mut cmd = std::process::Command::new("cmd.exe");
    cmd.args(["/c", &script.display().to_string()]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW — no console flash
    }
    cmd.spawn().context("failed to launch installer")?;
    Ok(format!("installing {}", p.display()))
}

// ---------------------------------------------------------------- launch purge

/// Dismiss the post-update "✓ updated" banner permanently.
pub fn ack_update_banner(dir: &Path) {
    let mut s = load_store(dir);
    if s.welcome_pending.is_some() {
        s.welcome_pending = None;
        save_store(dir, &s);
    }
}

/// Run once at startup: drop installed/superseded downloads and stale
/// .part/.tmp files (>24h).
pub fn launch_purge(dir: &Path) {
    let running = running_semver();
    let mut s = load_store(dir);
    let mut dirty = false;
    if let Some(d) = &s.downloaded {
        let stale = semver::Version::parse(&d.version)
            .map(|v| v <= running)
            .unwrap_or(true);
        if stale {
            // A staged installer equal to what now runs = it was just
            // installed via the updater — keep its notes and owe the
            // "✓ updated" banner even without a prior last_run record.
            if semver::Version::parse(&d.version).map(|v| v == running).unwrap_or(false) {
                if !d.notes.is_empty() {
                    s.installed_notes = d.notes.clone();
                }
                if s.welcome_pending.is_none() {
                    s.welcome_pending = Some(d.version.clone());
                }
            }
            let _ = std::fs::remove_file(&d.path);
            s.downloaded = None;
            dirty = true;
        }
    }
    if dirty {
        save_store(dir, &s);
    }
    let cutoff = SystemTime::now() - Duration::from_secs(24 * 3600);
    if let Ok(rd) = std::fs::read_dir(dl_dir(dir)) {
        for e in rd.flatten() {
            let p = e.path();
            let is_tmp = p
                .extension()
                .map(|x| x == "part" || x == "tmp")
                .unwrap_or(false);
            let old = e
                .metadata()
                .and_then(|m| m.modified())
                .map(|t| t < cutoff)
                .unwrap_or(false);
            if is_tmp && old {
                let _ = std::fs::remove_file(&p);
            }
        }
    }
}
