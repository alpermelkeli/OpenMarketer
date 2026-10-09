/**
 * Lists the API serves page by page: how the pages a screen has loaded become one list.
 *
 * A page is asked for with the previous page's `next_cursor`. Between two requests a
 * new item can arrive at the top of a list, which moves an item already seen onto the
 * next page; it is shown once, where it first appeared.
 */

/** The dashboard asks for fewer items than the API's default, so a long list loads in steps. */
export const PAGE_SIZE = 20;

export type Page = { next_cursor: string | null };

/** The cursor of the page after `last`, or nothing when `last` is the final page. */
export function nextCursor(last: Page): string | undefined {
  return last.next_cursor ?? undefined;
}

export function mergePages<Item>(pages: readonly (readonly Item[])[], keyOf: (item: Item) => string | number): Item[] {
  const seen = new Set<string | number>();
  const merged: Item[] = [];
  for (const item of pages.flat()) {
    const key = keyOf(item);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(item);
  }
  return merged;
}
