/** Formatting of identifiers and times for display. */

/** The first block of a UUID: enough to tell projects apart on a screen. */
export function shortId(id: string): string {
  return id.slice(0, 8);
}

const TITLE_MAX_LENGTH = 20;

/** The first twenty characters of a title, with an ellipsis when there was more. */
export function shortTitle(title: string): string {
  const characters = Array.from(title);
  return characters.length > TITLE_MAX_LENGTH
    ? `${characters.slice(0, TITLE_MAX_LENGTH).join("")}…`
    : title;
}

const DATE_TIME = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });

/** "Oct 8, 2026, 3:32 PM" in the reader's time zone, or the text as it came if it is not a time. */
export function formatDateTime(isoTime: string): string {
  const time = new Date(isoTime);
  return Number.isNaN(time.getTime()) ? isoTime : DATE_TIME.format(time);
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/**
 * How long before `now` a time was, to finish a sentence such as "Stored here …":
 * "just now", "5 minutes ago", "3 hours ago", "12 days ago", and after thirty days
 * the date itself, "on Oct 8, 2026, 3:32 PM". A time ahead of `now` is "just now":
 * the browser's clock may be behind the server's.
 */
export function formatTimeAgo(isoTime: string, now: number): string {
  const time = Date.parse(isoTime);
  if (Number.isNaN(time)) return isoTime;
  const elapsed = now - time;
  if (elapsed < MINUTE_MS) return "just now";
  if (elapsed < HOUR_MS) return ago(Math.floor(elapsed / MINUTE_MS), "minute");
  if (elapsed < DAY_MS) return ago(Math.floor(elapsed / HOUR_MS), "hour");
  if (elapsed < 30 * DAY_MS) return ago(Math.floor(elapsed / DAY_MS), "day");
  return `on ${formatDateTime(isoTime)}`;
}

function ago(count: number, unit: string): string {
  return `${count} ${unit}${count === 1 ? "" : "s"} ago`;
}
