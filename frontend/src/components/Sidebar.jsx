import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { Radar, LayoutDashboard, Satellite, Globe2, Images, ShieldAlert, Eye, Columns2, BookOpen, Map as MapIcon, HeartPulse, Users as UsersIcon, Bookmark, UserCog, BadgeCheck, ShieldCheck, CreditCard, ChevronsLeft, ChevronsRight, X } from "lucide-react";
import { api, hasRole } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";

const SECTIONS = [
  { title: "Operations", items: [
    { to: "/", label: "Surveillance", icon: LayoutDashboard, id: "nav-dashboard-link", end: true },
    { to: "/ingest", label: "Ingestion", icon: Satellite, id: "nav-ingest-link" },
    { to: "/explorer", label: "Scene Explorer", icon: Globe2, id: "nav-explorer-link" },
    { to: "/events", label: "Events", icon: Images, id: "nav-events-link" },
  ] },
  { title: "Investigation", items: [
    { to: "/alerts", label: "Alerts", icon: ShieldAlert, id: "nav-alerts-link" },
    { to: "/watchlist", label: "Watchlist", icon: Eye, id: "nav-watchlist-link" },
    { to: "/compare", label: "Compare", icon: Columns2, id: "nav-compare-link" },
  ] },
  { title: "Intelligence", items: [
    { to: "/archive", label: "Archive", icon: BookOpen, id: "nav-archive-link" },
    { to: "/zones", label: "Zones / Jurisdictions", icon: MapIcon, id: "nav-zones-link" },
    { to: "/validation", label: "Validation", icon: BadgeCheck, id: "nav-validation-link" },
    { to: "/health", label: "Data Sources", icon: HeartPulse, id: "nav-health-link" },
  ] },
];

const itemBase = "group relative flex items-center rounded-md py-2 text-sm outline-none transition-colors focus-visible:ring-1 focus-visible:ring-cyan-400/70";
const itemState = (isActive) => (isActive ? "bg-slate-800 text-cyan-300" : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-100");

const Tip = ({ label }) => (
  <span className="pointer-events-none absolute left-full z-50 ml-2 hidden whitespace-nowrap rounded bg-slate-900 px-2 py-1 font-mono text-[10px] text-slate-100 shadow-lg ring-1 ring-slate-700 group-hover:block group-focus-visible:block">{label}</span>
);

const NavItem = ({ item, collapsed, onNavigate }) => {
  const Icon = item.icon;
  return (
    <NavLink to={item.to} end={item.end} data-testid={item.id} onClick={onNavigate} title={collapsed ? item.label : undefined}
      className={({ isActive }) => `${itemBase} ${itemState(isActive)} ${collapsed ? "justify-center px-0" : "gap-3 px-3"} ${isActive ? "before:absolute before:left-0 before:top-1/2 before:h-5 before:w-0.5 before:-translate-y-1/2 before:rounded before:bg-cyan-400" : ""}`}>
      <Icon size={16} className="shrink-0" />
      {!collapsed && <span className="truncate">{item.label}</span>}
      {collapsed && <Tip label={item.label} />}
    </NavLink>
  );
};

const NavBody = ({ collapsed, onNavigate }) => {
  const { user } = useAuth();
  const [ref, setRef] = useState(null);
  useEffect(() => { api.get("/demo/reference").then((r) => setRef(r.data)).catch(() => setRef(null)); }, []);
  const refPinned = ref?.pinned && ref?.available;

  return (
    <nav className="flex flex-1 flex-col gap-4 overflow-y-auto overflow-x-hidden px-2 py-3 [&::-webkit-scrollbar]:w-1.5" data-testid="sidebar-nav">
      {SECTIONS.map((sec) => (
        <div key={sec.title} className="flex flex-col gap-0.5">
          {!collapsed && <p className="label-mono px-3 pb-1 text-[9px] text-slate-600">{sec.title}</p>}
          {sec.items.map((it) => <NavItem key={it.to} item={it} collapsed={collapsed} onNavigate={onNavigate} />)}
        </div>
      ))}

      <div className="mt-auto flex flex-col gap-1 border-t pt-3" style={{ borderColor: "var(--border-default)" }}>
        {!collapsed && refPinned && <p className="label-mono px-3 pb-1 text-[9px] text-slate-600">Reference</p>}
        {refPinned && <div title={collapsed ? (refPinned ? `Reference case pinned: ${ref?.case_number || ""}` : "Reference case not pinned") : undefined}
          className={`group relative flex items-center rounded-md py-1.5 ${collapsed ? "justify-center px-0" : "gap-3 px-3"}`} data-testid="sidebar-reference-status">
          <Bookmark size={15} className="shrink-0" style={{ color: refPinned ? "#22C55E" : "#64748B" }} />
          {!collapsed && (
            <span className="truncate font-mono text-[10px] leading-tight">
              <span className="text-slate-400">Reference case</span><br />
              {ref === null ? <span className="text-slate-500">checking…</span>
                : refPinned ? <span className="text-emerald-400" data-testid="sidebar-ref-pinned">✓ {ref.case_number || "Pinned"}</span>
                : <span className="text-slate-500" data-testid="sidebar-ref-unpinned">Not configured</span>}
            </span>
          )}
          {collapsed && <Tip label={refPinned ? `Reference: ${ref?.case_number || "pinned"}` : "Reference not pinned"} />}
        </div>}

        {hasRole(user, "admin") && (
          <>
            {!collapsed && <p className="label-mono px-3 pb-1 pt-2 text-[9px] text-slate-600">Admin</p>}
            <NavItem item={{ to: "/users", label: "Users & Roles", icon: UsersIcon, id: "nav-users-link" }} collapsed={collapsed} onNavigate={onNavigate} />
            <NavItem item={{ to: "/admin/security", label: "Security Center", icon: ShieldCheck, id: "nav-admin-security-link" }} collapsed={collapsed} onNavigate={onNavigate} />
          </>
        )}
        {!collapsed && <p className="label-mono px-3 pb-1 pt-2 text-[9px] text-slate-600">Session</p>}
        <NavItem item={{ to: "/billing", label: "Plans & Billing", icon: CreditCard, id: "nav-billing-link" }} collapsed={collapsed} onNavigate={onNavigate} />
        <NavItem item={{ to: "/account", label: "Account", icon: UserCog, id: "nav-account-sidebar-link" }} collapsed={collapsed} onNavigate={onNavigate} />
      </div>
    </nav>
  );
};

export const Sidebar = ({ collapsed, onToggleCollapse, mobileOpen, onCloseMobile }) => (
  <>
    {/* Desktop */}
    <aside className={`hidden shrink-0 flex-col border-r md:flex ${collapsed ? "w-[68px]" : "w-60"} transition-[width] duration-200`}
      style={{ borderColor: "var(--border-default)", background: "rgba(17,24,39,0.6)" }} data-testid="sidebar-desktop">
      <NavBody collapsed={collapsed} />
      <button data-testid="sidebar-collapse-toggle" onClick={onToggleCollapse} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        className="flex items-center justify-center gap-2 border-t py-2 text-slate-500 hover:bg-slate-800/60 hover:text-slate-200"
        style={{ borderColor: "var(--border-default)" }}>
        {collapsed ? <ChevronsRight size={16} /> : <><ChevronsLeft size={16} /><span className="font-mono text-[10px] uppercase tracking-wider">Collapse</span></>}
      </button>
    </aside>

    {/* Mobile drawer */}
    {mobileOpen && (
      <div className="fixed inset-0 z-50 md:hidden" data-testid="sidebar-mobile-overlay">
        <div className="absolute inset-0 bg-black/60" onClick={onCloseMobile} />
        <aside className="absolute left-0 top-0 flex h-full w-64 flex-col border-r" style={{ borderColor: "var(--border-default)", background: "var(--bg-primary)" }} data-testid="sidebar-mobile">
          <div className="flex h-14 shrink-0 items-center justify-between border-b px-4" style={{ borderColor: "var(--border-default)" }}>
            <span className="flex items-center gap-2 font-display text-base font-bold"><Radar size={16} color="#00F0FF" /> Varuna <span style={{ color: "#00F0FF" }}>Netra</span></span>
            <button data-testid="sidebar-mobile-close" onClick={onCloseMobile} className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"><X size={18} /></button>
          </div>
          <NavBody collapsed={false} onNavigate={onCloseMobile} />
        </aside>
      </div>
    )}
  </>
);
