import { useEffect, useState } from "react";
import {
  getToolConfig,
  openDir,
  pickServerDir,
  restartBackend,
  setServerConfig,
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

  const [rconEnabled, setRconEnabled] = useState(false);
  const [rconPort, setRconPort] = useState("25575");
  const [rconPass, setRconPass] = useState("");
  const [rconMsgs, setRconMsgs] = useState<string[]>([]);
  const [rconBusy, setRconBusy] = useState(false);

  const saveRcon = async () => {
    setRconBusy(true);
    setRconMsgs([]);
    try {
      const port = parseInt(rconPort, 10);
      if (!port || port < 1 || port > 65535) {
        setRconMsgs(["rcon port must be 1–65535"]);
        return;
      }
      const msgs = await setServerConfig(rconEnabled, port, rconPass.trim());
      setRconMsgs(msgs);
      setRconPass("");
    } catch (e) {
      setRconMsgs([String(e)]);
    } finally {
      setRconBusy(false);
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
        <div className="sect-h">RCON</div>
        <div className="btnrow">
          <label className="checkline">
            <input
              type="checkbox"
              checked={rconEnabled}
              onChange={(e) => setRconEnabled(e.target.checked)}
            />
            enable rcon
          </label>
          <input
            className="fld small"
            type="text"
            value={rconPort}
            onChange={(e) => setRconPort(e.target.value)}
            placeholder="port"
            spellCheck={false}
          />
        </div>
        <div className="btnrow">
          <input
            className="fld"
            type="password"
            value={rconPass}
            onChange={(e) => setRconPass(e.target.value)}
            placeholder="rcon password — blank keeps current"
            spellCheck={false}
          />
        </div>
        <div className="btnrow">
          <button className="act primary" disabled={rconBusy} onClick={saveRcon}>
            {rconBusy ? "applying…" : "Save & apply"}
          </button>
        </div>
        <div className="note">
          Writes the backend config AND server.properties (when it exists —
          brand-new servers create it on first boot), then restarts the
          backend so it takes effect immediately.
        </div>
        {rconMsgs.map((m, i) => (
          <div className="note" key={i}>• {m}</div>
        ))}
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
