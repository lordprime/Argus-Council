import { create } from "zustand";

import { api, auth, isUnauthorized, V1 } from "@/lib/api";
import type { TokenPair, User } from "@/lib/types";

interface SessionState {
  user: User | null;
  /**
   * `error`: the session check failed for a reason other than the session being invalid (rate limit,
   * server error, offline). The tokens are kept and the shell shows a retryable error (audit C-05).
   */
  status: "unknown" | "authenticated" | "anonymous" | "error";
  /** Why the last bootstrap failed, when `status` is `error`. */
  bootError: unknown;
  bootstrap: () => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

export const useSession = create<SessionState>((set, get) => ({
  user: null,
  status: "unknown",
  bootError: null,
  bootstrap: async () => {
    if (get().status === "error") set({ status: "unknown", bootError: null }); // a retry: show loading again
    if (!auth.hasSession()) {
      set({ status: "anonymous", bootError: null });
      return;
    }
    try {
      set({ user: await api.get<User>(`${V1}/auth/me`), status: "authenticated", bootError: null });
    } catch (err) {
      // Only a 401 (after the client's refresh attempt) means the session is gone. A 429, a 5xx or a
      // network failure says nothing about the tokens, so signing the user out would lose their place.
      if (isUnauthorized(err)) {
        auth.clear();
        set({ user: null, status: "anonymous", bootError: null });
      } else {
        set({ status: "error", bootError: err });
      }
    }
  },
  login: async (email, password) => {
    const tokens = await api.post<TokenPair>(`${V1}/auth/login`, { email, password });
    auth.setTokens(tokens);
    set({ user: await api.get<User>(`${V1}/auth/me`), status: "authenticated", bootError: null });
  },
  logout: () => {
    auth.clear();
    set({ user: null, status: "anonymous", bootError: null });
  },
}));

auth.onLost(() => useSession.setState({ user: null, status: "anonymous", bootError: null }));
