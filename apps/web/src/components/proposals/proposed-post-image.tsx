"use client";

import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { VersionImage } from "@/components/studio/version-image";
import { Skeleton } from "@/components/ui/skeleton";
import { useContentItem } from "@/lib/api/writing";
import type { Proposal } from "@/lib/api/types";
import { proposedImageOf, proposedImageToShow } from "@/lib/writing/attachments";

/**
 * The image a `publish_post` would go out with: the version its payload names, which is what a
 * person approves with the text. The payload does not name the image's asset, so its post is read
 * for that; a post that goes with another version is said, never shown in its place.
 */
export function ProposedPostImage({ proposal }: { proposal: Proposal }) {
  const proposed = proposedImageOf(proposal);
  const item = useContentItem(proposal.project_id, proposed.state === "named" ? proposed.contentItemId : null);

  if (proposed.state === "none") return null;
  if (proposed.state === "unplaceable") {
    return (
      <Notice tone="warning" title="The image cannot be shown">
        The proposal names image version <span className="font-mono break-all">{proposed.versionId}</span> but no post
        to find it by. Read the request below before you decide.
      </Notice>
    );
  }
  if (item.error !== null) {
    return (
      <RequestProblem
        title="The post's image could not be read"
        error={item.error}
        onRetry={() => void item.refetch()}
      />
    );
  }
  if (item.data === undefined) return <Skeleton className="aspect-square max-w-sm" />;

  const image = proposedImageToShow(proposed.versionId, item.data);
  if (image === null) {
    return (
      <Notice tone="warning" title="The post goes with another image than this proposal names">
        The proposal asks to publish image version <span className="font-mono break-all">{proposed.versionId}</span>,
        which is not the one the post holds. The run refuses to publish a post whose image differs from the one
        approved.
      </Notice>
    );
  }
  return (
    <VersionImage
      projectId={proposal.project_id}
      assetId={image.assetId}
      versionId={image.versionId}
      alt="The image the post would go out with, the version this proposal names"
      sizes="(min-width: 640px) 24rem, 100vw"
      className="max-w-sm overflow-hidden rounded-sm border border-dashed border-warning/60"
    />
  );
}
