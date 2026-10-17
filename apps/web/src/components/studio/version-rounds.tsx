import type { MediaAssetVersion } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { assetImageAlt } from "@/lib/studio/image-src";
import { roundsOf } from "@/lib/studio/reports";
import { formatBytes, formatCost, techniqueLabel } from "@/lib/studio/status";

import { VersionImage } from "./version-image";
import { VersionReviews } from "./version-reviews";

type VersionRoundsProps = {
  projectId: string;
  assetId: string;
  platform: string;
  versions: readonly MediaAssetVersion[];
  /** The version the asset stands for now, marked in its round. */
  currentVersionId: string | null;
};

/** Every round of the run, the first first: each version's image, how it was made, what it cost and what its reviews found. */
export function VersionRounds({ projectId, assetId, platform, versions, currentVersionId }: VersionRoundsProps) {
  const rounds = roundsOf(versions);
  return (
    <section aria-labelledby="rounds" className="grid gap-6 border-t pt-8">
      <div className="max-w-prose">
        <h2 id="rounds" className="font-heading text-lg">
          The rounds
        </h2>
        <p className="mt-1 text-sm text-pretty text-muted-foreground">
          Every image the run made, round by round, with how it was made and what the reviews found.
        </p>
      </div>
      <ol className="grid gap-10">
        {rounds.map(({ round, versions: made }) => (
          <li key={round} className="grid gap-4">
            <h3 className="font-heading text-base">Round {round}</h3>
            <ul className="grid gap-8">
              {made.map((version) => (
                <VersionItem
                  key={version.id}
                  projectId={projectId}
                  assetId={assetId}
                  platform={platform}
                  version={version}
                  current={version.id === currentVersionId}
                />
              ))}
            </ul>
          </li>
        ))}
      </ol>
    </section>
  );
}

type VersionItemProps = {
  projectId: string;
  assetId: string;
  platform: string;
  version: MediaAssetVersion;
  current: boolean;
};

function VersionItem({ projectId, assetId, platform, version, current }: VersionItemProps) {
  return (
    <li className="grid gap-4 sm:grid-cols-[10rem_minmax(0,1fr)] sm:gap-6">
      <div className="w-40 overflow-hidden rounded-sm border">
        <VersionImage
          projectId={projectId}
          assetId={assetId}
          versionId={version.id}
          alt={assetImageAlt(platform, { round: version.round })}
          size={{ width: version.width, height: version.height }}
          sizes="10rem"
        />
      </div>
      <div className="grid min-w-0 content-start gap-3">
        <div className="grid gap-0.5">
          <p className="text-sm">
            {techniqueLabel(version.technique)}
            {current && <span className="ml-2 text-2xs tracking-widest text-brand uppercase">The asset now</span>}
          </p>
          <p className="flex flex-wrap gap-x-3 font-mono text-xs text-muted-foreground tabular-nums">
            <span>
              {version.width} × {version.height}
            </span>
            <span>{formatBytes(version.size_bytes)}</span>
            <span>{version.mime}</span>
            <span aria-label="Cost">{formatCost(version.cost_usd)}</span>
            <span>{formatDateTime(version.created_at)}</span>
          </p>
          {(version.provider !== null || version.model !== null) && (
            <p className="text-xs text-muted-foreground">
              Made with <span className="font-mono">{[version.provider, version.model].filter(Boolean).join(" · ")}</span>
            </p>
          )}
          {version.has_code && (
            <p className="text-xs text-muted-foreground">
              Made by code in the sandbox ({version.code_chars.toLocaleString("en")} characters). The code is kept as a
              record and is not shown here.
            </p>
          )}
        </div>
        {version.prompt !== null && version.prompt !== "" && (
          <div className="grid gap-1">
            <p className="text-2xs tracking-widest text-muted-foreground uppercase">Prompt</p>
            {/* Written by a model: text only, never HTML or Markdown. */}
            <p className="max-w-prose text-sm text-pretty break-words whitespace-pre-wrap">{version.prompt}</p>
          </div>
        )}
        <VersionReviews version={version} />
      </div>
    </li>
  );
}
