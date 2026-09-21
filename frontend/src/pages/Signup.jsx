import { useEffect, useState } from "react";
import { Navigate, Link, useNavigate } from "react-router-dom";
import { Radar, UserPlus, Compass } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/context/AuthContext";
import { api, apiError } from "@/lib/api";

export default function Signup() {
  const { user, signup, guestLogin } = useAuth();
  const nav = useNavigate();
  const [f, setF] = useState({ name: "", email: "", password: "", confirm: "", organization: "" });
  const [busy, setBusy] = useState(false);
  const [exploreBusy, setExploreBusy] = useState(false);
  const [error, setError] = useState("");


  if (user) return <Navigate to="/" replace />;

  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const submit = async (e) => {
    e?.preventDefault();
    setError("");
    if (f.password !== f.confirm) { setError("Passwords do not match"); return; }
    setBusy(true);
    try {
      const u = await signup({ name: f.name, email: f.email, password: f.password, organization: f.organization || null });
      toast.success(`Welcome, ${u.name} — you have Viewer access`);
      nav("/", { replace: true });
    } catch (err) { setError(apiError(err)); } finally { setBusy(false); }
  };

  const explore = async () => {
    if (exploreBusy) return;
    setExploreBusy(true);
    try { await guestLogin(); nav("/", { replace: true }); } catch (err) { setError(apiError(err)); setExploreBusy(false); }
  };


  const inputCls = "w-full rounded border bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-400/60";
  return (
    <div className="grid h-screen grid-cols-1 lg:grid-cols-[1.1fr_1fr]" style={{ background: "var(--bg-primary)" }} data-testid="signup-page">
      <div className="hidden lg:flex flex-col justify-between p-12 grid-bg border-r" style={{ borderColor: "var(--border-default)" }}>
        <div className="flex items-center gap-2.5">
          <span className="grid h-9 w-9 place-items-center rounded-md" style={{ background: "rgba(0,240,255,0.12)", border: "1px solid rgba(0,240,255,0.4)" }}><Radar size={18} color="#00F0FF" /></span>
          <span className="font-display text-xl font-bold tracking-tight">Varuna <span style={{ color: "#00F0FF" }}>Netra</span></span>
        </div>
        <div className="max-w-lg fade-up">
          <p className="label-mono mb-3">Create your account</p>
          <h1 className="font-display text-4xl font-extrabold tracking-tight lg:text-5xl leading-[1.05]">Satellite spill detection, AIS correlation, auditable decisions.</h1>
          <p className="mt-5 text-sm leading-relaxed text-slate-400">New accounts receive read-only <span className="text-cyan-300">Viewer</span> access. Analyst and Supervisor access require administrator approval. Administrator access is never self-assigned.</p>
        </div>
        <div className="font-mono text-[11px] text-slate-500">Decision support · not a legal determination</div>
      </div>
      <div className="flex items-center justify-center p-8">
        <form onSubmit={submit} className="panel w-full max-w-md p-8 fade-up" data-testid="signup-form">
          <h2 className="font-display text-2xl font-bold tracking-tight">Create account</h2>
          <p className="mt-1 text-xs text-slate-400">Free Viewer access — explore every investigation, read-only.</p>
          <label className="mt-6 block"><span className="label-mono mb-1 block">Full name</span>
            <input data-testid="signup-name-input" value={f.name} onChange={set("name")} required className={inputCls} style={{ borderColor: "var(--border-highlight)" }} /></label>
          <label className="mt-3 block"><span className="label-mono mb-1 block">Email</span>
            <input data-testid="signup-email-input" type="email" autoComplete="username" value={f.email} onChange={set("email")} required className={inputCls} style={{ borderColor: "var(--border-highlight)" }} /></label>
          <label className="mt-3 block"><span className="label-mono mb-1 block">Organization / Institution (optional)</span>
            <input data-testid="signup-org-input" value={f.organization} onChange={set("organization")} className={inputCls} style={{ borderColor: "var(--border-highlight)" }} /></label>
          <label className="mt-3 block"><span className="label-mono mb-1 block">Password (min 10, letters + numbers)</span>
            <input data-testid="signup-password-input" type="password" autoComplete="new-password" value={f.password} onChange={set("password")} required className={inputCls} style={{ borderColor: "var(--border-highlight)" }} /></label>
          <label className="mt-3 block"><span className="label-mono mb-1 block">Confirm password</span>
            <input data-testid="signup-confirm-input" type="password" autoComplete="new-password" value={f.confirm} onChange={set("confirm")} required className={inputCls} style={{ borderColor: "var(--border-highlight)" }} /></label>
          {error && <p data-testid="signup-error" className="mt-3 rounded px-3 py-2 text-xs" style={{ color: "#FF2A6D", background: "rgba(255,42,109,0.1)", border: "1px solid rgba(255,42,109,0.4)" }}>{error}</p>}
          <button data-testid="signup-submit-button" disabled={busy} type="submit" className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded bg-cyan-400 px-4 py-2 font-mono text-xs font-semibold uppercase tracking-wider text-slate-950 hover:bg-cyan-300 disabled:opacity-50">
            <UserPlus size={14} /> {busy ? "Creating…" : "Create account"}
          </button>
          <div className="mt-5 flex items-center gap-3"><span className="h-px flex-1" style={{ background: "var(--border-default)" }} /><span className="label-mono">or</span><span className="h-px flex-1" style={{ background: "var(--border-default)" }} /></div>
          <button type="button" data-testid="signup-explore-button" onClick={explore} disabled={exploreBusy} className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded border px-4 py-2 font-mono text-xs font-semibold uppercase tracking-wider text-cyan-200 hover:bg-cyan-400/10 disabled:opacity-50" style={{ borderColor: "rgba(0,240,255,0.4)" }}>
            <Compass size={14} /> {exploreBusy ? "Entering…" : "Explore without an account"}
          </button>
          <Link to="/login" data-testid="have-account-link" className="mt-3 block text-center font-mono text-[11px] uppercase tracking-wider text-slate-400 hover:text-cyan-300">Already have an account? Sign in</Link>
        </form>
      </div>
    </div>
  );
}
