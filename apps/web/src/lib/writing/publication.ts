/**
 * What became of a post that was sent to the project's account: where it is once published, why
 * X refused it, and what to do about a send the run never confirmed. Display only: whether a post
 * is published, and why it was refused, is the server's to say; this words it.
 *
 * Every link is built from X's fixed origin and the platform's identifiers as encoded segments,
 * never taken from a stored address. Accounts are connected on X only, so X is named.
 */

import { accountProfileHref } from "@/lib/accounts/platforms";
import type { ContentItem, PublishingFailure } from "@/lib/api/types";

const X_ORIGIN = "https://x.com";

export type Publication = {
  /** The post on X, or null when the platform's id of it was not recorded. */
  href: string | null;
  /** The account it was published from, without `@`. */
  username: string | null;
  publishedAt: string | null;
  costUsd: number | null;
};

/** Where a published post is and what publishing it cost; null for a post that is not published. */
export function publicationOf(
  post: Pick<ContentItem, "status" | "external_id" | "published_username" | "published_at" | "publishing_cost_usd">,
): Publication | null {
  if (post.status !== "published") return null;
  return {
    href: postHref(post.external_id, post.published_username),
    username: post.published_username,
    publishedAt: post.published_at,
    costUsd: post.publishing_cost_usd,
  };
}

/** "https://x.com/name/status/123", or X's own address for a post by id when the username is unknown. */
export function postHref(externalId: string | null, username: string | null): string | null {
  if (externalId === null || externalId === "") return null;
  const account = username === null || username === "" ? "i" : encodeURIComponent(username);
  return `${X_ORIGIN}/${account}/status/${encodeURIComponent(externalId)}`;
}

export type FailureDisplay = {
  /** What X did, in a sentence. */
  title: string;
  /** What the person can do about it. */
  advice: string;
  /** Whether connecting the X account again is the way on, so the screen links to the accounts. */
  reconnect: boolean;
};

const FAILURES: Record<PublishingFailure, FailureDisplay> = {
  access_revoked: {
    title: "X refused access: reconnect the X account",
    advice: "The account's access was revoked or has run out. Connect it again, then start a new post.",
    reconnect: true,
  },
  duplicate: {
    title: "X refused it as a duplicate of an earlier post",
    advice: "X does not take the same text twice from one account. Start a new post with another angle.",
    reconnect: false,
  },
  too_long: {
    title: "X refused it as too long",
    advice: "X counted the post over the length it takes from this account. Start a new post.",
    reconnect: false,
  },
  media_refused: {
    title: "X refused the image",
    advice: "X did not take the image the post goes with. Start a new post with another image, or with none.",
    reconnect: false,
  },
  not_permitted: {
    title: "X did not let the app post",
    advice:
      "The X app's permission is read-only. Set it to Read and write in the app's settings on the X developer portal, then connect the X account again so its access carries the new permission.",
    reconnect: true,
  },
  out_of_credits: {
    title: "The X developer account is out of credits",
    advice: "X charges for every post. Add credits in the X developer console, then start a new post.",
    reconnect: false,
  },
  refused: {
    title: "X refused the post",
    advice: "X gave no reason the dashboard can name. Start a new post, or check the account on X.",
    reconnect: false,
  },
};

/** Why X refused a post, in words, with what to do; null for a post X did not refuse. */
export function publishingFailureDisplay(post: Pick<ContentItem, "status" | "publishing_failure">): FailureDisplay | null {
  if (post.status !== "failed" || post.publishing_failure === null) return null;
  return FAILURES[post.publishing_failure];
}

export type UnconfirmedPublishing = {
  text: string;
  /** The account's profile on X, where the reader can look for the post; null without a username. */
  profileHref: string | null;
};

/**
 * A send that started and was never confirmed: the post may be on the account or not, and only the
 * account tells. Not a failure: asking for it again could post it twice. Null for any other post.
 */
export function unconfirmedPublishing(
  post: Pick<ContentItem, "publishing_unconfirmed" | "published_username">,
): UnconfirmedPublishing | null {
  if (!post.publishing_unconfirmed) return null;
  const username = post.published_username;
  if (username === null || username === "") {
    return {
      text: "Publishing was never confirmed. The post may be on the X account — check its posts before asking for it again.",
      profileHref: null,
    };
  }
  return {
    text: `Publishing was never confirmed. The post may be on @${username} — check its posts before asking for it again.`,
    profileHref: accountProfileHref("x", username),
  };
}
