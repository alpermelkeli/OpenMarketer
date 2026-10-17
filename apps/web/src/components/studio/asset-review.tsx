"use client";

import { CheckIcon, DownloadIcon, LockIcon, XIcon } from "lucide-react";

import { StatusPill } from "@/components/common/status-pill";
import { Button, buttonVariants } from "@/components/ui/button";
import type { MediaAsset, MediaAssetVersion } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { assetImageAlt, versionDownloadHref } from "@/lib/studio/image-src";
import { assetStatusDisplay, formatCost, platformLabel } from "@/lib/studio/status";
import { cn } from "@/lib/utils";

import { RejectAssetDialog } from "./reject-asset-dialog";
import { VersionImage } from "./version-image";

type AssetReviewProps = {
  asset: MediaAsset;
  /** The version the asset stands for now; null when the API names none. */
  version: MediaAssetVersion | null;
  totalCostUsd: number;
  /** Whether the run has ended; the API refuses a decision before. */
  decidable: boolean;
  deciding: "accept" | "reject" | null;
  problem: string | null;
  onAccept: () => void;
  onReject: () => void;
  rejectOpen: boolean;
  onRejectOpenChange: (open: boolean) => void;
};

/**
 * The image the run ended with, large, with where it stands and the decision on it. A draft has a
 * dashed frame and says what accepting does; an accepted image has a solid frame and a lock.
 */
export function AssetReview({
  asset,
  version,
  totalCostUsd,
  decidable,
  deciding,
  problem,
  onAccept,
  onReject,
  rejectOpen,
  onRejectOpenChange,
}: AssetReviewProps) {
  const status = assetStatusDisplay(asset.status);
  const draft = asset.status === "draft";
  const download =
    version === null ? null : versionDownloadHref({ projectId: asset.project_id, assetId: asset.id, versionId: version.id });

  return (
    <section aria-labelledby="asset" className="grid gap-5 border-t pt-8">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h2 id="asset" className="font-heading text-lg">
          The image
        </h2>
        <StatusPill tone={status.tone}>
          {asset.status === "accepted" && <LockIcon aria-hidden="true" className="size-3" />}
          {status.label}
        </StatusPill>
        <p className="text-sm text-muted-foreground">{platformLabel(asset.platform)}</p>
      </div>

      {version === null ? (
        <p className="text-sm text-muted-foreground">The API names no current version of this asset.</p>
      ) : (
        <figure
          className={cn(
            "grid max-w-prose gap-2 rounded-md border p-2",
            draft && "border-dashed border-warning/60",
            asset.status === "accepted" && "border-success/50",
          )}
        >
          <VersionImage
            projectId={asset.project_id}
            assetId={asset.id}
            versionId={version.id}
            alt={assetImageAlt(asset.platform, { draft, round: version.round })}
            size={{ width: version.width, height: version.height }}
            sizes="(min-width: 768px) 38rem, 100vw"
            className="rounded-sm"
          />
          <figcaption className="flex flex-wrap gap-x-3 px-1 font-mono text-xs text-muted-foreground tabular-nums">
            <span>Round {version.round}</span>
            <span>
              {version.width} × {version.height}
            </span>
            <span>Every version together {formatCost(totalCostUsd)}</span>
          </figcaption>
        </figure>
      )}

      {draft && (
        <div className="grid max-w-prose gap-3">
          <p className="text-sm text-pretty text-muted-foreground">
            {decidable
              ? "This image is a draft. Accepting it marks it as fit to use and fixes it as it stands; rejecting it keeps it in the library as rejected. Neither can be undone, and neither posts anything."
              : "This image is a draft. It can be accepted or rejected once the run has ended."}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={onAccept} disabled={!decidable || deciding !== null || version === null}>
              <CheckIcon data-icon="inline-start" aria-hidden="true" />
              {deciding === "accept" ? "Accepting…" : "Accept"}
            </Button>
            <Button variant="outline" onClick={() => onRejectOpenChange(true)} disabled={!decidable || deciding !== null}>
              <XIcon data-icon="inline-start" aria-hidden="true" />
              Reject
            </Button>
            {download !== null && <DownloadLink href={download} />}
          </div>
          {problem !== null && !rejectOpen && (
            <p role="alert" className="text-sm text-pretty text-destructive">
              {problem}
            </p>
          )}
        </div>
      )}

      {!draft && (
        <div className="flex flex-wrap items-center gap-3">
          <p className="mr-auto text-sm text-muted-foreground">
            {asset.status === "accepted" ? "Accepted" : "Rejected"}
            {asset.decided_at !== null && ` ${formatDateTime(asset.decided_at)}`}. This decision is final.
          </p>
          {download !== null && <DownloadLink href={download} />}
        </div>
      )}

      <RejectAssetDialog
        open={rejectOpen}
        onOpenChange={onRejectOpenChange}
        pending={deciding === "reject"}
        problem={rejectOpen ? problem : null}
        onReject={onReject}
      />
    </section>
  );
}

function DownloadLink({ href }: { href: string }) {
  return (
    <a href={href} download className={buttonVariants({ variant: "ghost" })}>
      <DownloadIcon data-icon="inline-start" aria-hidden="true" />
      Download
    </a>
  );
}
