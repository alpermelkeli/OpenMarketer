"use client";

/**
 * The browser side of the stop-gap in `remembered.ts`: local storage as a store
 * React can subscribe to. Other tabs are followed through the `storage` event.
 */

import { useSyncExternalStore } from "react";

import { parseRemembered, type Remembered } from "./remembered";

const STORAGE_KEY = "openmarketer.dashboard.remembered.v1";

const listeners = new Set<() => void>();
let cache: { stored: string | null; value: Remembered } | null = null;

function readStored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    // Storage can be blocked (private mode, site settings); the dashboard then remembers nothing.
    return null;
  }
}

function snapshot(): Remembered {
  const stored = readStored();
  if (cache === null || cache.stored !== stored) {
    cache = { stored, value: parseRemembered(stored) };
  }
  return cache.value;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  window.addEventListener("storage", listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", listener);
  };
}

function update(change: (remembered: Remembered) => Remembered): void {
  const next = change(snapshot());
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    return;
  }
  listeners.forEach((listener) => listener());
}

export type RememberedStore = {
  /** `null` until the browser's storage has been read (during server rendering and hydration). */
  remembered: Remembered | null;
  update: (change: (remembered: Remembered) => Remembered) => void;
};

export function useRemembered(): RememberedStore {
  const remembered = useSyncExternalStore(subscribe, snapshot, () => null);
  return { remembered, update };
}
