import { ImageOffIcon } from "lucide-react";
import Link from "next/link";

import { FieldErrors } from "@/components/common/field-errors";
import { LoadMore } from "@/components/common/load-more";
import { StatusPill } from "@/components/common/status-pill";
import { VersionImage } from "@/components/studio/version-image";
import type { MediaAsset } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";
import { projectPageHref } from "@/lib/projects/project-navigation";
import { assetImageAlt } from "@/lib/studio/image-src";
import { assetStatusDisplay } from "@/lib/studio/status";
import { cn } from "@/lib/utils";
import type { Offered } from "@/lib/writing/brief-form";

type PostImageChoiceProps = {
  id: string;
  projectId: string;
  assets: Offered<MediaAsset>;
  /** The chosen asset's id; "" for a post alone. */
  value: string;
  onChange: (assetId: string) => void;
  problems: readonly string[];
  disabled: boolean;
  more: { hasMore: boolean; loading: boolean; onLoadMore: () => void };
};

const CHOICE =
  "flex cursor-pointer items-center gap-3 rounded-sm border p-2 text-sm transition-colors peer-checked:border-brand peer-checked:bg-brand/5 peer-focus-visible:ring-3 peer-focus-visible:ring-ring/50 peer-disabled:cursor-not-allowed peer-disabled:opacity-60";

/**
 * Which image of the project's Studio library the post goes with, if any: one choice per image,
 * each with its thumbnail and where it stands, after "No image". The writer does not see the image;
 * it is told what the image was asked to be, and the post waits for approval with it.
 */
export function PostImageChoice({
  id,
  projectId,
  assets,
  value,
  onChange,
  problems,
  disabled,
  more,
}: PostImageChoiceProps) {
  const name = `${id}-asset`;
  return (
    <fieldset className="grid gap-2" aria-describedby={`${id}-asset-hint ${id}-asset-errors`}>
      <legend className="mb-2 text-sm">An image to go with the post</legend>
      <ul className="grid gap-2 sm:grid-cols-2">
        <li>
          <input
            type="radio"
            id={`${name}-none`}
            name={name}
            value=""
            checked={value === ""}
            onChange={() => onChange("")}
            disabled={disabled}
            className="peer sr-only"
          />
          <label htmlFor={`${name}-none`} className={CHOICE}>
            <span className="grid size-14 shrink-0 place-items-center rounded-sm bg-surface text-muted-foreground">
              <ImageOffIcon aria-hidden="true" className="size-4" />
            </span>
            No image: the post is text alone
          </label>
        </li>
        {(assets.state === "known" ? assets.items : []).map((asset) => {
          const status = assetStatusDisplay(asset.status);
          const draft = asset.status === "draft";
          return (
            <li key={asset.id}>
              <input
                type="radio"
                id={`${name}-${asset.id}`}
                name={name}
                value={asset.id}
                checked={value === asset.id}
                onChange={() => onChange(asset.id)}
                disabled={disabled}
                className="peer sr-only"
              />
              <label htmlFor={`${name}-${asset.id}`} className={CHOICE}>
                {asset.current_version_id !== null && (
                  <VersionImage
                    projectId={projectId}
                    assetId={asset.id}
                    versionId={asset.current_version_id}
                    alt={assetImageAlt(asset.platform, { draft })}
                    sizes="3.5rem"
                    className={cn(
                      "size-14 shrink-0 overflow-hidden rounded-sm border",
                      draft && "border-dashed border-warning/60",
                    )}
                  />
                )}
                <span className="grid gap-1">
                  <StatusPill tone={status.tone}>{status.label}</StatusPill>
                  <span className="text-xs text-muted-foreground">
                    {formatDateTime(asset.created_at)} · <span className="font-mono">{shortId(asset.id)}</span>
                  </span>
                </span>
              </label>
            </li>
          );
        })}
      </ul>
      {assets.state === "reading" && <p className="text-xs text-muted-foreground">Reading the Studio library…</p>}
      <LoadMore noun="images" hasMore={more.hasMore} loading={more.loading} onLoadMore={more.onLoadMore} />
      <p id={`${id}-asset-hint`} className="text-xs text-pretty text-muted-foreground">
        {assets.state === "known" && assets.items.length === 0 && !more.hasMore ? (
          <>
            The{" "}
            <Link
              href={projectPageHref(projectId, "studio")}
              className="rounded-sm text-brand underline-offset-4 hover:underline"
            >
              Studio
            </Link>{" "}
            library has no image a post can go with yet.
          </>
        ) : (
          "Only images not rejected are offered. A draft image can be chosen: approving the post approves the image with it. The writer does not see the image; it writes from what the image was asked to be."
        )}
      </p>
      <FieldErrors id={`${id}-asset-errors`} messages={problems} />
    </fieldset>
  );
}
