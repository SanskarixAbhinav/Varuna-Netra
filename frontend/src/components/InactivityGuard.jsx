import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { useAuth } from "@/context/AuthContext";

const IDLE_MS = 10 * 60 * 1000;
const EVENTS = ["mousemove", "keydown", "click", "scroll", "touchstart"];

export const InactivityGuard = () => {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const timer = useRef(null);
  useEffect(() => {
    if (!user) return undefined;
    const arm = () => {
      clearTimeout(timer.current);
      timer.current = setTimeout(async () => {
        await logout();
        toast.warning("Session expired after 10 minutes of inactivity — please sign in again.", { duration: 10000 });
        nav("/login", { replace: true });
      }, IDLE_MS);
    };
    EVENTS.forEach((e) => window.addEventListener(e, arm, { passive: true }));
    arm();
    return () => { clearTimeout(timer.current); EVENTS.forEach((e) => window.removeEventListener(e, arm)); };
  }, [user, logout, nav]);
  return null;
};
