import { useEffect, useState } from "react";
import { enable, isEnabled, disable } from "@tauri-apps/plugin-autostart";
import { openDir, tailBackendLog, type Overview } from "../lib/api";

export default function AppView({ ov }: { ov: Overview | null }) {
  const [auto, setAuto] = useState(false);
  const [log, setLog] = useState<string[]>([]);

  useEffect(() => {
    isEnabled().then(setAuto).catch(() => {});
  }, []);

  const refreshLog = () => {
    tailBackendLog(60).then(setLog).catch(() => setLog([]));
  };
  useEffect(refreshLog, []);

  const toggleAuto = async () => {
    try {
      if (auto) {
        await disable();
      } else {
        await enable();
      }
      setAuto(!auto);
    } catch {
      /* plugin reports via isEnabled on next load */
    }
  };

  return (
    <div className="view">
      <h2>APP</h2>
      <p className="sub">this tool's own settings</p>

      <div className="sect">
        <div className="sect-h">Startup</div>
        <label className="checkline">
          <input type="checkbox" checked={auto} onChange={toggleAuto} />
          launch EcoHost when I log in
        </label>
        <div className="note">the backend starts with the app — no separate step needed</div>
      </div>

      <div className="sect">
        <div className="sect-h">App data</div>
        <div className="row">
          <span className="k">location</span>
          <span className="fill" />
          <span className="v dim">{ov?.app_dir ?? "—"}</span>
        </div>
        <div className="btnrow">
          <button className="act" onClick={() => ov && openDir(ov.app_dir)}>Open folder</button>
        </div>
        <div className="note">
          auth.db, config.json, player stats, update downloads, logs — all live here
        </div>
      </div>

      <div className="sect">
        <div className="sect-h">
          Backend log <button className="act ghost" style={{ marginLeft: 8, padding: "1px 8px", fontSize: 10 }} onClick={refreshLog}>refresh</button>
        </div>
        <div className="logtail">
          {log.length ? log.join("\n") : "— no output captured yet —"}
        </div>
        <div className="note">{ov?.backend_log}</div>
      </div>
    </div>
  );
}
