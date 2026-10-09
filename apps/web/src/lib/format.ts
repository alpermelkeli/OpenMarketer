/** Formatting of identifiers and times for display. */

/** The first block of a UUID: enough to tell projects apart on a screen. */
export function shortId(id: string): string {
  return id.slice(0, 8);
}

const DATE_TIME = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });

/** "Oct 8, 2026, 3:32 PM" in the reader's time zone, or the text as it came if it is not a time. */
export function formatDateTime(isoTime: string): string {
  const time = new Date(isoTime);
  return Number.isNaN(time.getTime()) ? isoTime : DATE_TIME.format(time);
}
