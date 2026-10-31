import { useEffect, useState, type JSX } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { getVersion } from "@tauri-apps/api/app";
import {
  appUpdateStatus,
  getOverview,
  type AppUpdateStatus,
  type Overview,
} from "./lib/api";
import OverviewView from "./views/OverviewView";
import ServerView from "./views/ServerView";
import UpdatesView from "./views/UpdatesView";
import AppView from "./views/AppView";

type View = "overview" | "server" | "updates" | "app";

const NAV: { id: View; label: string; icon: JSX.Element }[] = [
  {
    id: "overview",
    label: "OVERVIEW",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <rect x="3" y="4" width="18" height="12" rx="1" />
        <path d="M8 20h8M12 16v4" />
      </svg>
    ),
  },
  {
    id: "server",
    label: "SERVER",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <rect x="3" y="4" width="18" height="6" rx="1" />
        <rect x="3" y="14" width="18" height="6" rx="1" />
        <path d="M7 7h.01M7 17h.01" />
      </svg>
    ),
  },
  {
    id: "updates",
    label: "UPDATES",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M21 12a9 9 0 11-9-9" />
        <path d="M21 3v6h-6" />
        <path d="M12 8v4l3 2" />
      </svg>
    ),
  },
  {
    id: "app",
    label: "APP",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.7 1.7 0 00.3 1.9l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.9-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.2a1.7 1.7 0 00-1-1.5 1.7 1.7 0 00-1.9.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.9 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.2a1.7 1.7 0 001.5-1 1.7 1.7 0 00-.3-1.9l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.9.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.2a1.7 1.7 0 001 1.5 1.7 1.7 0 001.9-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.9v.1a1.7 1.7 0 001.5 1h.2a2 2 0 110 4h-.2a1.7 1.7 0 00-1.5 1z" />
      </svg>
    ),
  },
];

export default function App() {
  const [view, setView] = useState<View>("overview");
  const [ov, setOv] = useState<Overview | null>(null);
  const [upd, setUpd] = useState<AppUpdateStatus | null>(null);
  const [ver, setVer] = useState("");

  useEffect(() => {
    getVersion().then(setVer).catch(() => {});
  }, []);

  // Overview poll — the whole status card is just this.
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const o = await getOverview();
        if (alive) setOv(o);
      } catch {
        /* backend mid-restart; next tick recovers */
      }
    };
    poll();
    const t = setInterval(poll, 2500);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // Update poll — only to flag the rail dot + welcome banner; the view
  // owns its own faster poll while open.
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const s = await appUpdateStatus();
        if (alive) setUpd(s);
      } catch {
        /* offline etc. */
      }
    };
    poll();
    const t = setInterval(poll, 30000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const win = getCurrentWindow();
  const up = ov?.backend_up ?? false;
  const offerDot = upd?.state === "offer" || upd?.state === "downloaded";

  const drag = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest("button")) return;
    if (e.buttons === 1) win.startDragging();
  };

  return (
    <>
      <div className="titlebar" onMouseDown={drag} onDoubleClick={(e) => {
        if (!(e.target as HTMLElement).closest("button")) win.toggleMaximize();
      }}>
        <span className="mark">E</span>
        <span className="name">ECOHOST</span>
        <span className={`pill${up ? " up" : ""}`}>
          {up ? "backend :8000" : "backend …"}
        </span>
        <div className="drag" />
        <div className="winbtns">
          <button onClick={() => win.minimize()} title="Minimize">–</button>
          <button onClick={() => win.toggleMaximize()} title="Maximize">□</button>
          <button className="x" onClick={() => win.close()} title="Close">×</button>
        </div>
      </div>

      <div className="shell">
        <nav className="rail">
          {NAV.map((n) => (
            <button
              key={n.id}
              className={view === n.id ? "on" : ""}
              onClick={() => setView(n.id)}
            >
              {n.icon}
              {n.label}
              <span className={`dot${n.id === "updates" && offerDot ? " show" : ""}`} />
            </button>
          ))}
        </nav>

        {view === "overview" && <OverviewView ov={ov} />}
        {view === "server" && <ServerView ov={ov} />}
        {view === "updates" && <UpdatesView />}
        {view === "app" && <AppView ov={ov} />}
      </div>

      <div className="statusbar">
        <span>
          <span className={`sq${up ? " on" : ""}`} />
          {up
            ? `mc: ${ov?.public_status?.mc_state ?? "?"} · ${ov?.public_status?.players_online ?? 0} online`
            : "backend starting…"}
        </span>
        {upd?.justUpdated && <span>✓ updated to v{upd.justUpdated}</span>}
        <span className="r">EcoHost v{ver}</span>
      </div>
    </>
  );
}
