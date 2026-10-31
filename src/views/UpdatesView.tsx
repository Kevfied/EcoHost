import { useEffect, useRef, useState } from "react";
import {
  ackAppUpdate,
  appUpdateStatus,
  cancelAppUpdate,
  downloadAppUpdate,
  formatSize,
  installAppUpdate,
  skipAppVersion,
  unskipAppVersion,
  type AppUpdateStatus,
} from "../lib/api";

export default function UpdatesView() {
  const [s, setS] = useState<AppUpdateStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const dlWanted = useRef("");

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const st = await appUpdateStatus();
        if (!alive) return;
        // An in-flight poll must not snap the card back to "offer" while a
        // download request is still being handed to Rust.
        if (dlWanted.current && st.state === "offer" && st.version === dlWanted.current) {
          st.state = "downloading";
        }
        setS(st);
      } catch {
        /* next tick */
      }
    };
    poll();
    const t = setInterval(poll, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const download = async (version: string) => {
    dlWanted.current = version;
    setBusy(true);
    try {
      await downloadAppUpdate();
    } catch {
      dlWanted.current = "";
    } finally {
      setBusy(false);
    }
  };

  const install = async () => {
    setBusy(true);
    try {
      await installAppUpdate(); // app exits on success — update.cmd takes over
    } finally {
      setBusy(false);
    }
  };

  const st = s?.state ?? "idle";
  const pct = s ? Math.round((s.progress || 0) * 100) : 0;

  return (
    <div className="view">
      <h2>UPDATES</h2>
      <p className="sub">signed release manifests from GitHub Releases</p>

      {s?.justUpdated && (
        <div className="upd">
          <div className="upd-v">✓ updated to v{s.justUpdated}</div>
          {s.updatedNotes && <div className="upd-notes">{s.updatedNotes}</div>}
          <div className="btnrow">
            <button className="act" onClick={() => { ackAppUpdate(); setS({ ...s, justUpdated: "" }); }}>
              dismiss
            </button>
          </div>
        </div>
      )}

      <div className="sect">
        <div className="sect-h">Current</div>
        <div className="row">
          <span className="k">app version</span>
          <span className="fill" />
          <span className="v">v{s?.runningVersion ?? "…"}</span>
        </div>
      </div>

      {st === "offer" && (
        <div className="upd">
          <div className="upd-v">v{s!.version} available</div>
          {s!.notes && <div className="upd-notes">{s!.notes}</div>}
          <div className="upd-size">{formatSize(s!.size)}</div>
          <div className="btnrow">
            <button className="act primary" disabled={busy} onClick={() => download(s!.version)}>
              Download
            </button>
            <button className="act ghost" onClick={() => skipAppVersion(s!.version)}>
              skip this version
            </button>
          </div>
        </div>
      )}

      {st === "downloading" && (
        <div className="upd">
          <div className="upd-v">downloading… {pct}%</div>
          <div className="prog"><div style={{ width: `${pct}%` }} /></div>
          <div className="btnrow">
            <button className="act ghost" onClick={cancelAppUpdate}>cancel</button>
          </div>
        </div>
      )}

      {st === "downloaded" && (
        <div className="upd">
          <div className="upd-v">✓ v{s!.version} ready to install</div>
          {s!.notes && <div className="upd-notes">{s!.notes}</div>}
          <div className="btnrow">
            <button className="act primary" disabled={busy} onClick={install}>
              Install &amp; restart
            </button>
          </div>
          <div className="note">the app closes, installs silently, and relaunches itself</div>
        </div>
      )}

      {st === "skipped" && (
        <div className="upd">
          <div className="upd-v">v{s!.version} skipped</div>
          <div className="btnrow">
            <button className="act" onClick={unskipAppVersion}>un-skip</button>
          </div>
        </div>
      )}

      {st === "idle" && !s?.justUpdated && (
        <div className="note">
          {s?.error ? `update check failed: ${s.error}` : "no update available — you're on the latest"}
        </div>
      )}
      {s?.error && st !== "idle" && <div className="note err">{s.error}</div>}
    </div>
  );
}
