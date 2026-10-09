import { describe, expect, it } from "vitest";

import { mergePages, nextCursor } from "./pages";

const id = (item: { id: string }) => item.id;

describe("pages of a list", () => {
  it("become one list in the order they were loaded", () => {
    const pages = [[{ id: "c" }, { id: "b" }], [{ id: "a" }]];
    expect(mergePages(pages, id).map(id)).toEqual(["c", "b", "a"]);
  });

  it("show an item once when a new arrival pushed it onto the next page", () => {
    const pages = [[{ id: "c" }, { id: "b" }], [{ id: "b" }, { id: "a" }]];
    expect(mergePages(pages, id).map(id)).toEqual(["c", "b", "a"]);
  });

  it("are an empty list before anything has loaded", () => {
    expect(mergePages([], id)).toEqual([]);
    expect(mergePages([[]], id)).toEqual([]);
  });

  it("end when the API gives no cursor", () => {
    expect(nextCursor({ next_cursor: "abc" })).toBe("abc");
    expect(nextCursor({ next_cursor: null })).toBeUndefined();
  });
});
