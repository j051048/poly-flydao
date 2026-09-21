"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export type ToastTone = "success" | "error" | "info";

export interface ToastMessage {
  id: number;
  tone: ToastTone;
  text: string;
}

const DEFAULT_DURATION_MS = 6_000;

/**
 * Toast state for operator actions.
 *
 * Control actions used to report only through inline text, which is easy to
 * miss when the relevant panel has scrolled out of view. The hook keeps the
 * list and auto-dismiss timers; ``ToastViewport`` renders it. A page-local hook
 * (instead of a global provider) keeps every page rendering without a wrapper
 * and avoids cross-route leakage of transient messages.
 */
export function useToasts(durationMs: number = DEFAULT_DURATION_MS) {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const push = useCallback(
    (text: string, tone: ToastTone = "info") => {
      if (!text) return;
      const id = nextId.current++;
      setToasts((current) => [...current.slice(-2), { id, tone, text }]);
      timers.current.set(
        id,
        setTimeout(() => {
          timers.current.delete(id);
          setToasts((current) => current.filter((toast) => toast.id !== id));
        }, durationMs),
      );
    },
    [durationMs],
  );

  useEffect(
    () => () => {
      timers.current.forEach((timer) => clearTimeout(timer));
      timers.current.clear();
    },
    [],
  );

  return useMemo(() => ({ toasts, push, dismiss }), [toasts, push, dismiss]);
}

export default function ToastViewport({
  toasts,
  onDismiss,
}: {
  toasts: ToastMessage[];
  onDismiss: (id: number) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="toast-viewport" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div className={`toast toast-${toast.tone}`} key={toast.id}>
          <span>{toast.text}</span>
          <button
            type="button"
            className="toast-close"
            aria-label="关闭提示"
            onClick={() => onDismiss(toast.id)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
