/**
 * How an analysis run is shown: its label, its tone, whether it can still change,
 * and how long it has taken. Display only; the run's state is the server's.
 */

import type { AnalysisRun, AnalysisRunStatus } from "@/lib/api/types";

export type Tone = "neutral" | "progress" | "success" | "warning" | "danger";

type StatusDisplay = { label: string; tone: Tone };

const STATUS_DISPLAY: Record<AnalysisRunStatus, StatusDisplay> = {
  queued: { label: "Queued", tone: "neutral" },
  running: { label: "Running", tone: "progress" },
  succeeded: { label: "Succeeded", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
};

export function runStatusDisplay(status: AnalysisRunStatus): StatusDisplay {
  return STATUS_DISPLAY[status];
}

/** A finished run never changes again, so it does not need to be polled. */
export function isRunFinished(run: Pick<AnalysisRun, "status">): boolean {
  return run.status === "succeeded" || run.status === "failed";
}

/**
 * Milliseconds from the request to the end of the run, or to `now` while it is
 * unfinished. Never negative: the browser's clock may be behind the server's.
 */
export function runElapsedMs(
  run: Pick<AnalysisRun, "created_at" | "finished_at">,
  now: number,
): number {
  const start = Date.parse(run.created_at);
  const end = run.finished_at === null ? now : Date.parse(run.finished_at);
  const elapsed = end - start;
  return Number.isFinite(elapsed) && elapsed > 0 ? elapsed : 0;
}

/** "8s", "1m 05s", "2h 03m". */
export function formatDuration(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  const seconds = totalSeconds % 60;
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const hours = Math.floor(totalSeconds / 3600);
  if (hours > 0) return `${hours}h ${pad(minutes)}m`;
  if (minutes > 0) return `${minutes}m ${pad(seconds)}s`;
  return `${seconds}s`;
}

/** After this long in `queued`, the likeliest reason is that no worker is running. */
export const QUEUED_TOO_LONG_MS = 20_000;

export function isWaitingForWorker(run: Pick<AnalysisRun, "status" | "created_at" | "finished_at">, now: number): boolean {
  return run.status === "queued" && runElapsedMs(run, now) >= QUEUED_TOO_LONG_MS;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}
