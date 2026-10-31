import { useState } from "react";
import {
  formatDuration,
  openDashboard,
  restartBackend,
  stopBackend,
  type Overview,
} from "../lib/api";

export default function OverviewView({ ov }: { ov: Overview | null }) {
  const [busy, setBusy] = useState("");
  const ps = ov?.public_status;

  const act = async (name: string, f: () => Promise<unknown>) => {
    setBusy(name);
    try {
      await f();
    } catch {
      /* surfaced by next poll */
    } finally {
      setBusy("");
    }
  };

  const up = ov?.backend_up ?? false;
  const mcState = ps?.mc_state ?? "—";

  return (
    <div className="view">
      <h2>OVERVIEW</h2>
      <p className="sub">backend + minecraft server at a glance</p>

      <div className="sect">
        <div className="sect-h">Backend</div>
        <div className="row">
          <span className="k">status</span>
          <span className="fill" />
          <span className="v">{up ? "listening :8000" : "starting…"}</span>
        </div>
        <div className="row">
          <span className="k">process</span>
          <span className="fill" />
          <span className="v dim">
            {ov ? (ov.managed ? "managed by this app" : "external (reused)") : "—"}
          </span>
        </div>
        <div className="row">
          <span className="k">backend version</span>
          <span className="fill" />
          <span className="v dim">{ps?.version ?? "—"}</span>
        </div>
      </div>

      <div className="sect">
        <div className="sect-h">Minecraft Server</div>
        <div className="row">
          <span className="k">state</span>
          <span className="fill" />
          <span className="v">{mcState}</span>
        </div>
        <div className="row">
          <span className="k">players online</span>
          <span className="fill" />
          <span className="v">
            {ps ? `${ps.players_online}${ps.players.length ? ` — ${ps.players.join(", ")}` : ""}` : "—"}
          </span>
        </div>
        <div className="row">
          <span className="k">server uptime</span>
          <span className="fill" />
          <span className="v dim">{ps ? formatDuration(ps.mc_uptime_seconds) : "—"}</span>
        </div>
        <div className="row">
          <span className="k">cpu / ram</span>
          <span className="fill" />
          <span className="v dim">
            {ps ? `${ps.cpu_usage.toFixed(0)}% / ${ps.ram_usage.toFixed(0)}%` : "—"}
          </span>
        </div>
        <div className="row">
          <span className="k">power mode</span>
          <span className="fill" />
          <span className="v dim">{ps?.power_mode ?? "—"}</span>
        </div>
      </div>

      <div className="sect">
        <div className="sect-h">Actions</div>
        <div className="btnrow">
          <button className="act primary" disabled={!up || busy !== ""} onClick={() => act("dash", openDashboard)}>
            Open Dashboard ↗
          </button>
          <button className="act" disabled={busy !== ""} onClick={() => act("restart", restartBackend)}>
            {busy === "restart" ? "restarting…" : "Restart backend"}
          </button>
          <button className="act ghost" disabled={!ov?.managed || busy !== ""} onClick={() => act("stop", stopBackend)}>
            Stop backend
          </button>
        </div>
        <div className="note">
          "Stop" only works on a backend this app spawned — an externally running
          backend (your own <code>python main.py</code>) is left alone.
        </div>
      </div>
    </div>
  );
}
