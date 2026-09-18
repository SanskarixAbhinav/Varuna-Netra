import { useNavigate } from "react-router-dom";
import { Bell, BellOff, Siren, X, Wifi, RefreshCw } from "lucide-react";
import { useLive } from "@/context/LiveFeed";

export const LiveBell = () => {
  const live = useLive();
  const nav = useNavigate();
  if (!live) return null;
  return (
    <button data-testid="live-bell" onClick={() => { live.clearUnread(); nav("/alerts"); }} title={`Live feed: ${live.mode}`} className="relative inline-flex items-center gap-1.5 rounded px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-slate-300 hover:text-white">
      <Bell size={14} />
      {live.unread > 0 && <span data-testid="live-bell-count" className="absolute -right-1 -top-1 grid h-4 min-w-4 place-items-center rounded-full px-1 text-[9px] font-bold text-slate-950" style={{ background: "#FF2A6D" }}>{live.unread}</span>}
      <span data-testid="live-mode" className="hidden lg:inline" style={{ color: live.mode === "live" ? "#10B981" : live.mode === "polling" ? "#FFB703" : "#64748B" }}>{live.mode === "live" ? <Wifi size={10} className="inline" /> : <RefreshCw size={10} className="inline" />} {live.mode}</span>
    </button>
  );
};

export const CriticalBanner = () => {
  const live = useLive();
  const nav = useNavigate();
  if (!live?.critical) return null;
  const a = live.critical;
  return (
    <div data-testid="critical-banner" className="critical-banner flex items-center gap-3 px-5 py-2 text-xs" role="alert">
      <Siren size={16} className="shrink-0 animate-pulse" />
      <span className="font-mono text-[10px] font-bold uppercase tracking-[0.2em]">Critical</span>
      <span className="truncate">{a.message}</span>
      {a.case_id && <button data-testid="critical-banner-open" onClick={() => { live.dismissCritical(); nav(`/cases/${a.case_id}`); }} className="ml-auto shrink-0 rounded bg-white px-3 py-1 font-mono text-[10px] font-bold uppercase tracking-wider text-rose-700">Acknowledge &amp; respond</button>}
      <button data-testid="critical-banner-mute" onClick={live.toggleMute} title={live.muted ? "unmute siren" : "mute siren"} className="shrink-0 rounded p-1 hover:bg-white/20">{live.muted ? <BellOff size={13} /> : <Bell size={13} />}</button>
      <button data-testid="critical-banner-dismiss" onClick={live.dismissCritical} className="shrink-0 rounded p-1 hover:bg-white/20"><X size={13} /></button>
    </div>
  );
};
