import { describe, expect, it } from "vitest";

import type { ContentItem, PublishingFailure } from "@/lib/api/types";

import { postHref, publicationOf, publishingFailureDisplay, unconfirmedPublishing } from "./publication";

type Sent = Pick<
  ContentItem,
  | "status"
  | "external_id"
  | "published_username"
  | "published_at"
  | "publishing_cost_usd"
  | "publishing_failure"
  | "publishing_unconfirmed"
>;

function post(changes: Partial<Sent>): Sent {
  return {
    status: "published",
    external_id: "1850000000000000000",
    published_username: "openmarketer",
    published_at: "2026-11-10T09:30:00Z",
    publishing_cost_usd: 0.015,
    publishing_failure: null,
    publishing_unconfirmed: false,
    ...changes,
  };
}

describe("a published post", () => {
  it("links to the post on X, with the account, the time and the cost", () => {
    expect(publicationOf(post({}))).toEqual({
      href: "https://x.com/openmarketer/status/1850000000000000000",
      username: "openmarketer",
      publishedAt: "2026-11-10T09:30:00Z",
      costUsd: 0.015,
    });
  });

  it("is nothing for a post that is not published", () => {
    expect(publicationOf(post({ status: "publishing" }))).toBeNull();
    expect(publicationOf(post({ status: "dry_run" }))).toBeNull();
  });
});

describe("the link to a post on X", () => {
  it("is built from X's origin and encoded segments", () => {
    expect(postHref("12/../34", "a b")).toBe("https://x.com/a%20b/status/12%2F..%2F34");
  });

  it("uses X's own address for a post by id when the username is unknown", () => {
    expect(postHref("123", null)).toBe("https://x.com/i/status/123");
  });

  it("is none without the post's id", () => {
    expect(postHref(null, "openmarketer")).toBeNull();
    expect(postHref("", "openmarketer")).toBeNull();
  });
});

describe("a post X refused", () => {
  function failure(reason: PublishingFailure) {
    return publishingFailureDisplay(post({ status: "failed", publishing_failure: reason }));
  }

  it("asks to reconnect the account when X refused access", () => {
    expect(failure("access_revoked")).toMatchObject({
      title: "X refused access: reconnect the X account",
      reconnect: true,
    });
  });

  it("says a duplicate is one", () => {
    expect(failure("duplicate")?.title).toBe("X refused it as a duplicate of an earlier post");
    expect(failure("duplicate")?.reconnect).toBe(false);
  });

  it("says a read-only app needs Read and write, and the account connected again", () => {
    const display = failure("not_permitted");
    expect(display?.advice).toMatch(/Read and write/);
    expect(display?.reconnect).toBe(true);
  });

  it("says credits are added in the X developer console", () => {
    expect(failure("out_of_credits")?.advice).toMatch(/X developer console/);
  });

  it("has words for every reason X gives", () => {
    for (const reason of ["too_long", "media_refused", "refused"] as const) {
      expect(failure(reason)?.title).toMatch(/^X refused/);
    }
  });

  it("is nothing for a post X did not refuse", () => {
    expect(publishingFailureDisplay(post({ status: "failed", publishing_failure: null }))).toBeNull();
    expect(publishingFailureDisplay(post({}))).toBeNull();
  });
});

describe("a send never confirmed", () => {
  it("warns that the post may be on the account, and links to the account's posts", () => {
    expect(unconfirmedPublishing(post({ status: "publishing", publishing_unconfirmed: true }))).toEqual({
      text: "Publishing was never confirmed. The post may be on @openmarketer — check its posts before asking for it again.",
      profileHref: "https://x.com/openmarketer",
    });
  });

  it("names no account it does not know", () => {
    const unconfirmed = unconfirmedPublishing(
      post({ status: "publishing", publishing_unconfirmed: true, published_username: null }),
    );
    expect(unconfirmed?.profileHref).toBeNull();
    expect(unconfirmed?.text).toMatch(/on the X account/);
  });

  it("is nothing for a send that was confirmed", () => {
    expect(unconfirmedPublishing(post({}))).toBeNull();
  });
});
