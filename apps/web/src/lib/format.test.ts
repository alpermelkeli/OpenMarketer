import { describe, expect, it } from "vitest";

import { formatTimeAgo, shortTitle } from "./format";

const THEN = "2026-10-08T12:00:00Z";
const after = (ms: number) => Date.parse(THEN) + ms;
const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

describe("how long ago a time was", () => {
  it("is just now within the first minute", () => {
    expect(formatTimeAgo(THEN, after(0))).toBe("just now");
    expect(formatTimeAgo(THEN, after(59 * SECOND))).toBe("just now");
  });

  it("is just now when the browser's clock is behind", () => {
    expect(formatTimeAgo(THEN, after(-5 * MINUTE))).toBe("just now");
  });

  it("counts whole minutes, hours and days", () => {
    expect(formatTimeAgo(THEN, after(MINUTE))).toBe("1 minute ago");
    expect(formatTimeAgo(THEN, after(59 * MINUTE + 59 * SECOND))).toBe("59 minutes ago");
    expect(formatTimeAgo(THEN, after(HOUR))).toBe("1 hour ago");
    expect(formatTimeAgo(THEN, after(23 * HOUR))).toBe("23 hours ago");
    expect(formatTimeAgo(THEN, after(DAY))).toBe("1 day ago");
    expect(formatTimeAgo(THEN, after(29 * DAY))).toBe("29 days ago");
  });

  it("is the date itself after thirty days", () => {
    expect(formatTimeAgo(THEN, after(30 * DAY))).toMatch(/^on Oct \d{1,2}, 2026, /);
  });

  it("is the text as it came when it is not a time", () => {
    expect(formatTimeAgo("not a time", after(0))).toBe("not a time");
  });
});

describe("how a title is shortened for a list", () => {
  it("keeps a title of twenty characters as it is", () => {
    expect(shortTitle("Pricing page reviews")).toBe("Pricing page reviews");
  });

  it("cuts a longer title after twenty characters and marks it with an ellipsis", () => {
    expect(shortTitle("How does the pricing page work")).toBe("How does the pricing…");
  });

  it("does not split a character at the cut", () => {
    expect(shortTitle(`${"x".repeat(19)}😀 and more`)).toBe(`${"x".repeat(19)}😀…`);
  });
});
