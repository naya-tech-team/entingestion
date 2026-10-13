import { useEffect, useRef } from "react";

/** Calls `callback` every `delayMs` while delayMs is not null, skipping while the tab is hidden. */
export function useInterval(callback: () => void, delayMs: number | null): void {
  const saved = useRef(callback);
  useEffect(() => {
    saved.current = callback;
  }, [callback]);
  useEffect(() => {
    if (delayMs === null) return undefined;
    const id = setInterval(() => {
      if (typeof document === "undefined" || !document.hidden) saved.current();
    }, delayMs);
    return () => clearInterval(id);
  }, [delayMs]);
}
