import { create } from "zustand";

export interface Toast {
  id: number;
  tone: "info" | "success" | "warning" | "danger";
  title: string;
  body?: string;
  action?: { label: string; run: () => void };
}

interface ToastState {
  toasts: Toast[];
  push: (t: Omit<Toast, "id">) => void;
  dismiss: (id: number) => void;
}

let next = 1;
export const useToasts = create<ToastState>((set) => ({
  toasts: [],
  push: (t) => {
    const id = next++;
    set((s) => ({ toasts: [...s.toasts.slice(-3), { ...t, id }] }));
    setTimeout(() => set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) })), t.tone === "danger" ? 8000 : 4500);
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) })),
}));

export const toast = (t: Omit<Toast, "id">) => useToasts.getState().push(t);
