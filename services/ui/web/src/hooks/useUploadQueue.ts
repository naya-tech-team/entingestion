import { useCallback, useMemo, useState } from "react";
import { addEntries, queueProgress, type UploadEntry } from "../upload";

let nextId = 0;
const newId = () => `f${++nextId}`;

export interface UploadQueue {
  entries: UploadEntry[];
  /** The entries that passed validation (the ones the batch sends). */
  valid: UploadEntry[];
  progress: { done: number; failed: number; total: number };
  addFiles: (files: FileList | File[]) => void;
  update: (id: string, patch: Partial<UploadEntry>) => void;
  remove: (id: string) => void;
  clear: () => void;
}

/** The upload page's file list: add (deduplicated and validated), edit, remove, clear, progress. */
export function useUploadQueue(): UploadQueue {
  const [entries, setEntries] = useState<UploadEntry[]>([]);

  const addFiles = useCallback((files: FileList | File[]) => {
    setEntries((current) => addEntries(current, Array.from(files), newId));
  }, []);
  const update = useCallback((id: string, patch: Partial<UploadEntry>) => {
    setEntries((current) => current.map((e) => (e.id === id ? { ...e, ...patch } : e)));
  }, []);
  const remove = useCallback((id: string) => setEntries((current) => current.filter((e) => e.id !== id)), []);
  const clear = useCallback(() => setEntries([]), []);

  const valid = useMemo(() => entries.filter((e) => !e.error), [entries]);
  const progress = useMemo(() => queueProgress(entries), [entries]);

  return { entries, valid, progress, addFiles, update, remove, clear };
}
