import { useEffect, useState } from "react";
import {
  getToolConfig,
  openDir,
  pickServerDir,
  restartBackend,
  setToolConfig,
  type Overview,
} from "../lib/api";

export default function ServerView({ ov }: { ov: Overview | null }) {
  const [dir, setDir] = useState("");
  const [saved, setSaved] = useState("");
  const [needsRestart, setNeedsRestart] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getToolConfig()
      .then((c) => setDir(c.server_dir))
      .catch(() => {});
  }, []);

  const browse = async () => {
    const p = await pickServerDir().catch(() => null);
    if (p) setDir(p);
  };

  const save = async () => {
    setBusy(true);
    try {
      const restart = await setToolConfig({ server_dir: dir });
      setNeedsRestart(restart);
      setSaved("saved");
      setTimeout(() => setSaved(""), 2000);
    } finally {
      setBusy(false);
    }
  };

  const doRestart = async () => {
    setBusy(true);
    try {
      await restartBackend();
      setNeedsRestart(false);
    } finally {
      setBusy(false);
    }
  };

  const liveDir = ov?.managed ? ov.spawned_dir : ov?.public_status?.server_dir;

  return (
    <div className="view">
      <h2>SERVER</h2>
      <p className="sub">things the web dashboard can't change — they belong to this process</p>

      <div className="sect">
        <div className="sect-h">Minecraft server directory</div>
        <div className="row">
          <span className="k">in use now</span>
          <span className="fill" />
          <span className="v">{liveDir || "—"}</span>
        </div>
        <div className="btnrow">
          <input
            className="fld"
            type="text"
            value={dir}
            onChange={(e) => setDir(e.target.value)}
            placeholder="C:\Path\To\Minecraft\Server"
            spellCheck={false}
          />
          <button className="act" onClick={browse}>Browse…</button>
        </div>
        <div className="btnrow">
          <button className="act primary" disabled={busy} onClick={save}>
            {saved || "Save"}
          </button>
          {needsRestart && (
            <button className="act" disabled={busy} onClick={doRestart}>
              Restart backend to apply
            </button>
          )}
        </div>
        <div className="note">
          Passed to the backend as ECOHOST_SERVER_DIR on spawn. A running
          backend keeps its old directory until restarted.
        </div>
        {needsRestart && (
          <div className="note err">server dir changed — restart required to take effect</div>
        )}
      </div>

      <div className="sect">
        <div className="sect-h">Folders</div>
        <div className="btnrow">
          <button className="act" disabled={!dir} onClick={() => openDir(dir)}>
            Open server folder
          </button>
          <button className="act" onClick={() => ov && openDir(ov.app_dir)}>
            Open app data folder
          </button>
        </div>
        <div className="note">
          app data = %LOCALAPPDATA%\EcoHost (config.json, auth.db, stats, logs)
        </div>
      </div>
    </div>
  );
}
