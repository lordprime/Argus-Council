import { create } from "zustand";

import { api, auth, isUnauthorized, V1 } from "@/lib/api";
import type { MfaChallenge, TokenPair, User } from "@/lib/types";

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
  /**
   * Password step. Resolves with a challenge when the account has two-factor authentication on;
   * finish with `verifyMfa`. Otherwise the session starts right away.
   */
  login: (email: string, password: string) => Promise<MfaChallenge | undefined>;
  verifyMfa: (mfaToken: string, code: string) => Promise<void>;
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
    const res = await api.post<TokenPair | MfaChallenge>(`${V1}/auth/login`, { email, password });
    if ("mfa_required" in res && res.mfa_required) return res;
    auth.setTokens(res as TokenPair);
    set({ user: await api.get<User>(`${V1}/auth/me`), status: "authenticated", bootError: null });
    return undefined;
  },
  verifyMfa: async (mfaToken, code) => {
    const tokens = await api.post<TokenPair>(`${V1}/auth/mfa/verify`, { mfa_token: mfaToken, code });
    auth.setTokens(tokens);
    set({ user: await api.get<User>(`${V1}/auth/me`), status: "authenticated", bootError: null });
  },
  logout: () => {
    auth.revoke();
    set({ user: null, status: "anonymous", bootError: null });
  },
}));

auth.onLost(() => useSession.setState({ user: null, status: "anonymous", bootError: null }));
