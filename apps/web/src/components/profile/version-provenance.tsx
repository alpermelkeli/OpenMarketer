import Link from "next/link";

import type { ProfileVersion } from "@/lib/api/types";
import { linkableHost, commitOrNull } from "@/lib/profile/evidence-link";
import { shortCommit } from "@/lib/profile/version-labels";

type VersionProvenanceProps = {
  version: ProfileVersion;
  repositoryUrl: string;
  /** What differs from the version this one was edited from; absent while that version loads or when there is none. */
  changesFromParent: readonly string[] | null;
};

/** Where the version on screen comes from: an analysis or an edit, which commit, and what the edit changed. */
export function VersionProvenance({ version, repositoryUrl, changesFromParent }: VersionProvenanceProps) {
  const parent = version.edited_from_version;
  const host = linkableHost(repositoryUrl);
  const pinned = commitOrNull(version.commit_sha) !== null;

  return (
    <div className="grid max-w-prose gap-3 text-sm text-pretty text-muted-foreground">
      <p>
        {parent === null ? (
          "Drafted by an analysis of the repository. "
        ) : (
          <>
            Edited from{" "}
            <Link
              href={`/projects/${version.project_id}/profile?version=${parent}`}
              scroll={false}
              className="rounded-sm text-brand underline-offset-4 hover:underline"
            >
              version {parent}
            </Link>
            {". "}
          </>
        )}
        {version.commit_sha === null ? (
          "The commit its evidence refers to is not recorded."
        ) : (
          <>
            Its evidence refers to the repository at commit{" "}
            <span className="font-mono text-foreground" title={version.commit_sha}>
              {shortCommit(version.commit_sha)}
            </span>
            .
          </>
        )}
      </p>

      {parent !== null && changesFromParent !== null && (
        <div>
          <p>
            {changesFromParent.length === 0
              ? `Its content is the same as version ${parent}.`
              : `Differs from version ${parent} in:`}
          </p>
          {changesFromParent.length > 0 && (
            <ul className="mt-1 list-disc pl-4 text-foreground marker:text-border-strong">
              {changesFromParent.map((change) => (
                <li key={change} className="break-words">
                  {change}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <p className="text-xs">
        {host === null
          ? "Evidence is shown as file and lines: this repository is not on a host the dashboard can link to."
          : pinned
            ? `Evidence is a file and its lines. Each reference opens the file on ${host} at the analysed commit, as the analyzer read it.`
            : `Evidence is a file and its lines. The analysed commit is not recorded for this version, so each reference opens the file on ${host} on the default branch as it is today, where the lines may have moved.`}
      </p>
    </div>
  );
}
