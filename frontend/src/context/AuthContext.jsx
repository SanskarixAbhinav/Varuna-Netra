import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";

const AuthCtx = createContext(null);

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null); // null = checking, false = anonymous

  useEffect(() => {
    api.get("/auth/me").then((r) => setUser(r.data)).catch(() => setUser(false));
    const onUnauth = () => setUser(false);
    window.addEventListener("sentinelmar:unauthorized", onUnauth);
    return () => window.removeEventListener("sentinelmar:unauthorized", onUnauth);
  }, []);

  const login = useCallback(async (email, password) => {
    const { data } = await api.post("/auth/login", { email, password });
    setUser(data.user);
    return data.user;
  }, []);

  const guestLogin = useCallback(async () => {
    const { data } = await api.post("/auth/guest");
    setUser(data.user);
    return data.user;
  }, []);

  const signup = useCallback(async (payload) => {
    const { data } = await api.post("/auth/signup", payload);
    setUser(data.user);
    return data.user;
  }, []);

  const refreshUser = useCallback(async () => {
    const { data } = await api.get("/auth/me");
    setUser(data);
    return data;
  }, []);


  const logout = useCallback(async () => {
    try { await api.post("/auth/logout"); } catch (error) { console.warn("AuthContext: server logout failed, clearing local session anyway", error); }
    setUser(false);
  }, []);

  const value = useMemo(() => ({ user, login, guestLogin, signup, refreshUser, logout }), [user, login, guestLogin, signup, refreshUser, logout]);
  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
};

export const useAuth = () => useContext(AuthCtx);
