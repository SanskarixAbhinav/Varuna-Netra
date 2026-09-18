import { useEffect, useState } from "react";
import { Outlet, useNavigate, NavLink, useLocation } from "react-router-dom";
import { Radar, ShieldAlert, LogOut, Menu, MapPin } from "lucide-react";
import { api } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { LiveBell, CriticalBanner } from "@/components/LiveBell";
import { Sidebar } from "@/components/Sidebar";

const ROLE_COLOR = { guest: "#94A3B8", viewer: "#38BDF8", analyst: "#00F0FF", supervisor: "#FFB703", admin: "#FF2A6D" };
const COLLAPSE_KEY = "vn_sidebar_collapsed";

export const Layout = () => {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const [clock, setClock] = useState(new Date());
  const [stats, setStats] = useState(null);
  const [statsErr, setStatsErr] = useState(false);
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem(COLLAPSE_KEY) === "1"; } catch { return false; } });
  const [mobileOpen, setMobileOpen] = useState(false);

  const toggleCollapse = () => setCollapsed((c) => { const n = !c; try { localStorage.setItem(COLLAPSE_KEY, n ? "1" : "0"); } catch { /* ignore */ } return n; });

  useEffect(() => {
    const t = setInterval(() => setClock(new Date()), 1000);
    const load = () => api.get("/dashboard/summary").then((r) => { setStats(r.data); setStatsErr(false); }).catch(() => setStatsErr(true));
    load();
    const s = setInterval(load, 15000);
    const onEvt = () => load();
    window.addEventListener("varuna:refresh-counters", onEvt);
    return () => { clearInterval(t); clearInterval(s); window.removeEventListener("varuna:refresh-counters", onEvt); };
  }, []);

  return (
    <div className="flex h-screen flex-col overflow-hidden text-slate-100" style={{ background: "var(--bg-primary)" }}>
      <header className="flex h-14 shrink-0 items-center gap-3 border-b px-4" style={{ borderColor: "var(--border-default)", background: "rgba(17,24,39,0.85)", backdropFilter: "blur(12px)" }}>
        <button data-testid="mobile-menu-button" onClick={() => setMobileOpen(true)} className="rounded p-1.5 text-slate-300 hover:bg-slate-800 md:hidden"><Menu size={18} /></button>
        <NavLink to="/" data-testid="nav-brand" className="flex shrink-0 items-center gap-2.5">
          <span className="grid h-8 w-8 place-items-center rounded-md" style={{ background: "rgba(0,240,255,0.12)", border: "1px solid rgba(0,240,255,0.4)" }}>
            <Radar size={16} color="#00F0FF" />
          </span>
          <span className="whitespace-nowrap font-display text-lg font-bold tracking-tight">Varuna <span style={{ color: "#00F0FF" }}>Netra</span></span>
        </NavLink>
        <ContextChip />
        <div className="ml-auto flex shrink-0 items-center gap-4">
          {(stats || statsErr) && (
            <div className="hidden items-center gap-4 xl:flex" title="Real database counts (demo/mock records excluded)">
              <Stat label="Active cases" value={statsErr ? "Unavailable" : stats.active_cases} testId="nav-stat-cases" onClick={() => nav("/?origin=real")} />
              <Stat label="Pending" value={statsErr ? "Unavailable" : stats.pending_review} color="#FFB703" testId="nav-stat-pending" onClick={() => nav("/?origin=real&view=pending")} />
              <Stat label="Alerts" value={statsErr ? "Unavailable" : stats.alerts.unread} color="#FF2A6D" icon={<ShieldAlert size={12} />} testId="nav-stat-alerts" onClick={() => nav("/alerts?alerts=unread")} />
              {stats?.demo?.imported > 0 && <Stat label="Imported" value={stats.demo.imported} color="#FFB703" testId="nav-stat-imported" onClick={() => nav("/?origin=imported")} />}
              {stats?.demo?.cases > 0 && <Stat label="Demo" value={stats.demo.cases} color="#94A3B8" testId="nav-stat-demo" onClick={() => nav("/?origin=demo")} />}
            </div>
          )}
          {!stats && !statsErr && (
            <div className="hidden items-center gap-4 xl:flex" data-testid="nav-stats-loading">
              <Stat label="Cases" value="—" /><Stat label="Pending" value="—" color="#FFB703" /><Stat label="Alerts" value="—" color="#FF2A6D" />
            </div>
          )}
          <div className="hidden shrink-0 items-center gap-2 whitespace-nowrap font-mono text-xs text-slate-300 sm:flex" data-testid="utc-clock">
            <span className="pulse-dot" />
            {clock.toISOString().replace("T", " ").slice(0, 19)} UTC
          </div>
          <LiveBell />
          {user && (
            <div className="flex items-center gap-2 border-l pl-4" style={{ borderColor: "var(--border-default)" }} data-testid="user-chip">
              {user.role === "guest" && <span data-testid="guest-badge" className="hidden rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider sm:inline" style={{ color: "#94A3B8", borderColor: "#94A3B855", background: "rgba(148,163,184,0.1)" }}>Guest · read only</span>}
              <NavLink to="/account" data-testid="nav-account-link" className="hidden text-right leading-tight sm:block hover:opacity-80">
                <div className="text-xs text-slate-200" data-testid="user-name">{user.name}</div>
                <div className="font-mono text-[10px] uppercase tracking-wider" style={{ color: ROLE_COLOR[user.role] || "#94A3B8" }} data-testid="user-role">{user.role}</div>
              </NavLink>
              <button data-testid="logout-button" onClick={async () => { await logout(); nav("/login"); }} title="Sign out" className="rounded p-1.5 text-slate-400 hover:bg-slate-800 hover:text-slate-100"><LogOut size={14} /></button>
            </div>
          )}
        </div>
      </header>
      <CriticalBanner />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar collapsed={collapsed} onToggleCollapse={toggleCollapse} mobileOpen={mobileOpen} onCloseMobile={() => setMobileOpen(false)} />
        <main className="flex-1 overflow-hidden">
          <Outlet />
        </main>
      </div>
    </div>
  );
};

const Stat = ({ label, value, color = "#F8FAFC", icon, testId, onClick }) => (
  <button type="button" onClick={onClick} className={`flex items-baseline gap-1.5 ${onClick ? "cursor-pointer hover:opacity-80" : "cursor-default"}`} data-testid={testId}>
    <span className="label-mono">{label}</span>
    <span className="font-mono text-sm font-semibold flex items-center gap-1" style={{ color }}>{icon}{value}</span>
  </button>
);

const ContextChip = () => {
  const loc = useLocation();
  const [label, setLabel] = useState(null);
  const [tone, setTone] = useState("#38BDF8");
  useEffect(() => {
    let cancel = false;
    const m = loc.pathname.match(/^\/cases\/([^/]+)/);
    if (m) {
      setTone("#00F0FF");
      api.get(`/cases/${m[1]}`).then((r) => { if (!cancel) setLabel(`CASE ${r.data.case_number}`); }).catch(() => { if (!cancel) setLabel("CASE"); });
    } else {
      setTone("#38BDF8");
      api.get("/aoi").then((r) => { if (!cancel) { const a = r.data?.aoi; setLabel(a ? `AOI · ${a.name || a.label || a.kind || "custom"}` : "AOI · Global (none set)"); } }).catch(() => { if (!cancel) setLabel(null); });
    }
    return () => { cancel = true; };
  }, [loc.pathname]);
  if (!label) return null;
  return (
    <div title={label} data-testid="context-chip" className="hidden max-w-[260px] items-center gap-1.5 rounded-full border px-3 py-1 lg:flex" style={{ borderColor: "var(--border-default)", background: "rgba(56,189,248,0.06)" }}>
      <MapPin size={12} color={tone} className="shrink-0" />
      <span className="truncate font-mono text-[11px]" style={{ color: tone }}>{label}</span>
    </div>
  );
};
