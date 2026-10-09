import { describe, expect, it } from "vitest";

import {
  formatDuration,
  isRunFinished,
  isWaitingForWorker,
  runElapsedMs,
  runStatusDisplay,
} from "./run-status";

const requested = "2026-10-08T12:00:00Z";
const at = (seconds: number) => Date.parse(requested) + seconds * 1000;

describe("run status", () => {
  it("has a label for every status", () => {
    expect(runStatusDisplay("queued").label).toBe("Queued");
    expect(runStatusDisplay("running").label).toBe("Running");
    expect(runStatusDisplay("succeeded").label).toBe("Succeeded");
    expect(runStatusDisplay("failed").label).toBe("Failed");
  });

  it("is finished only when succeeded or failed", () => {
    expect(isRunFinished({ status: "queued" })).toBe(false);
    expect(isRunFinished({ status: "running" })).toBe(false);
    expect(isRunFinished({ status: "succeeded" })).toBe(true);
    expect(isRunFinished({ status: "failed" })).toBe(true);
  });
});

describe("elapsed time", () => {
  it("runs to now while the run is unfinished", () => {
    expect(runElapsedMs({ created_at: requested, finished_at: null }, at(42))).toBe(42_000);
  });

  it("stops at the end of a finished run", () => {
    const run = { created_at: requested, finished_at: "2026-10-08T12:01:05Z" };
    expect(runElapsedMs(run, at(9999))).toBe(65_000);
  });

  it("is zero when the browser's clock is behind the server's", () => {
    expect(runElapsedMs({ created_at: requested, finished_at: null }, at(-5))).toBe(0);
  });

  it("is zero for a time that cannot be read", () => {
    expect(runElapsedMs({ created_at: "not a time", finished_at: null }, at(5))).toBe(0);
  });

  it("is formatted in the largest two units", () => {
    expect(formatDuration(8_400)).toBe("8s");
    expect(formatDuration(65_000)).toBe("1m 05s");
    expect(formatDuration(7_380_000)).toBe("2h 03m");
  });
});

describe("a run that waits for a worker", () => {
  it("is a queued run that has waited long", () => {
    const run = { status: "queued" as const, created_at: requested, finished_at: null };
    expect(isWaitingForWorker(run, at(5))).toBe(false);
    expect(isWaitingForWorker(run, at(25))).toBe(true);
  });

  it("is never a run that has started", () => {
    const run = { status: "running" as const, created_at: requested, finished_at: null };
    expect(isWaitingForWorker(run, at(500))).toBe(false);
  });
});
