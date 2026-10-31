/**
 * What a post goes with besides its text, as its page and its proposal show it: the version of a
 * Studio image it was proposed with, and the research run whose findings it drew on. Display
 * only: whether the image may go with the post, and which version is approved, is the server's.
 */

import type { ContentItem, Proposal } from "@/lib/api/types";

export type PostImage = { assetId: string; versionId: string };

/** The image the post was proposed with, or null for a post alone. */
export function postImageOf(post: Pick<ContentItem, "asset_id" | "asset_version_id">): PostImage | null {
  if (post.asset_id === null || post.asset_version_id === null) return null;
  return { assetId: post.asset_id, versionId: post.asset_version_id };
}

/**
 * The image a writing run's `publish_post` asks to publish with its text, as far as the proposal
 * says: what a person approves is the version named in the payload. The payload names the post's
 * content item but not the image's asset, so the screen reads the item for that.
 */
export type ProposedImage =
  /** The proposal asks for no image, or is not a post's. */
  | { state: "none" }
  /** The payload names a version; the post that names its asset is read from `contentItemId`. */
  | { state: "named"; versionId: string; contentItemId: string }
  /** The payload names a version but no post to find its asset by. */
  | { state: "unplaceable"; versionId: string };

const WRITING_RUN = "writing-run";

export function proposedImageOf(proposal: Pick<Proposal, "action_name" | "run_kind" | "payload">): ProposedImage {
  if (proposal.action_name !== "publish_post" || proposal.run_kind !== WRITING_RUN) return { state: "none" };
  const { asset_version_id: versionId, content_item_id: contentItemId } = proposal.payload;
  if (typeof versionId !== "string" || versionId === "") return { state: "none" };
  if (typeof contentItemId !== "string" || contentItemId === "") return { state: "unplaceable", versionId };
  return { state: "named", versionId, contentItemId };
}

/**
 * The image to show for a proposal once its post is read: the payload's version, under the asset
 * the post names. Null when the post goes with another version than the payload asks for, which
 * the worker would refuse to publish: the screen says so instead of showing either.
 */
export function proposedImageToShow(
  versionId: string,
  post: Pick<ContentItem, "asset_id" | "asset_version_id">,
): PostImage | null {
  const image = postImageOf(post);
  return image !== null && image.versionId === versionId ? image : null;
}
