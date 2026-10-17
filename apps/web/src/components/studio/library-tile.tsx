import { ImageIcon } from "lucide-react";
import Link from "next/link";

import { Spinner } from "@/components/common/spinner";
import { StatusPill } from "@/components/common/status-pill";
import { formatDateTime } from "@/lib/format";
import { studioHref } from "@/lib/projects/project-navigation";
import { assetImageAlt } from "@/lib/studio/image-src";
import { tileState, tileVersionId, type LibraryTile as Tile } from "@/lib/studio/library";
import { formatCost, platformLabel } from "@/lib/studio/status";
import { cn } from "@/lib/utils";

import { VersionImage } from "./version-image";

type LibraryTileProps = { projectId: string; tile: Tile };

/**
 * One image of the library: its thumbnail once there is one, where it stands in words, the
 * platform it is for and what its run cost. A draft has a dashed frame; an accepted one, solid.
 */
export function LibraryTile({ projectId, tile }: LibraryTileProps) {
  const state = tileState(tile);
  const versionId = tileVersionId(tile);
  const runId = tile.run?.id ?? tile.asset?.run_id ?? null;
  const platform = tile.run?.platform ?? tile.asset?.platform ?? "";
  const draft = tile.asset?.status === "draft";
  const createdAt = tile.run?.created_at ?? tile.asset?.created_at;

  const body = (
    <>
      <div
        className={cn(
          "overflow-hidden rounded-sm border",
          draft && "border-dashed border-warning/60",
          tile.asset?.status === "accepted" && "border-success/50",
        )}
      >
        {versionId !== null && tile.asset !== null ? (
          <VersionImage
            projectId={projectId}
            assetId={tile.asset.id}
            versionId={versionId}
            alt={assetImageAlt(platform, { draft })}
            sizes="(min-width: 1024px) 18rem, (min-width: 640px) 45vw, 100vw"
          />
        ) : (
          <div className="grid aspect-square place-items-center bg-surface text-muted-foreground">
            {state.live ? <Spinner className="size-5" /> : <ImageIcon aria-hidden="true" className="size-5" />}
            <span className="sr-only">{state.live ? "The image is being made" : "No image to show"}</span>
          </div>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill tone={state.tone} live={state.live}>
          {state.label}
        </StatusPill>
        {state.limitsReached && <StatusPill tone="warning">Limits reached</StatusPill>}
      </div>
      <p className="text-sm">{platformLabel(platform)}</p>
      <p className="flex flex-wrap gap-x-2 text-xs text-muted-foreground">
        {createdAt !== undefined && <span>{formatDateTime(createdAt)}</span>}
        {tile.run !== null && <span className="font-mono tabular-nums">{formatCost(tile.run.cost_usd)}</span>}
      </p>
    </>
  );

  return (
    <li>
      {runId === null ? (
        <div className="grid gap-2">{body}</div>
      ) : (
        <Link
          href={studioHref(projectId, runId)}
          className="grid gap-2 rounded-sm outline-offset-4 transition-opacity hover:opacity-85"
        >
          {body}
        </Link>
      )}
    </li>
  );
}
