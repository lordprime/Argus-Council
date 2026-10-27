import { create } from "zustand";

import { api, auth, V1 } from "@/lib/api";
import type { TokenPair, User } from "@/lib/types";

interface SessionState {
  user: User | null;
  status: "unknown" | "authenticated" | "anonymous";
  bootstrap: () => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

export const useSession = create<SessionState>((set) => ({
  user: null,
  status: "unknown",
  bootstrap: async () => {
    if (!auth.hasSession()) {
      set({ status: "anonymous" });
      return;
    }
    try {
      set({ user: await api.get<User>(`${V1}/auth/me`), status: "authenticated" });
    } catch {
      auth.clear();
      set({ user: null, status: "anonymous" });
    }
  },
  login: async (email, password) => {
    const tokens = await api.post<TokenPair>(`${V1}/auth/login`, { email, password });
    auth.setTokens(tokens);
    set({ user: await api.get<User>(`${V1}/auth/me`), status: "authenticated" });
  },
  logout: () => {
    auth.revoke();
    set({ user: null, status: "anonymous" });
  },
}));

auth.onLost(() => useSession.setState({ user: null, status: "anonymous" }));
