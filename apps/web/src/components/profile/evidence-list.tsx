import { ArrowUpRightIcon, FileIcon } from "lucide-react";

import type { Evidence } from "@/lib/api/types";
import { evidenceLink, type EvidenceSource } from "@/lib/profile/evidence-link";

type EvidenceListProps = {
  evidence: readonly Evidence[];
  source: EvidenceSource;
};

/**
 * The files and lines a claim rests on. Paths are written by a model reading an
 * untrusted repository: they are shown as text and linked only through
 * `evidenceLink`, which builds the address itself.
 */
export function EvidenceList({ evidence, source }: EvidenceListProps) {
  if (evidence.length === 0) {
    return <p className="text-xs text-warning">No evidence: nothing in the repository was cited for this.</p>;
  }
  return (
    <ul aria-label="Evidence" className="grid gap-1">
      {evidence.map((item, index) => {
        const link = evidenceLink(source.repositoryUrl, item, source.commitSha);
        const reference = (
          <>
            <FileIcon aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
            <span className="break-all">{item.file}</span>
            {item.lines && <span className="shrink-0 tabular-nums">lines {item.lines}</span>}
          </>
        );
        return (
          <li key={`${item.file}:${item.lines ?? ""}:${index}`} className="font-mono text-xs text-muted-foreground">
            {link === null ? (
              <span className="inline-flex items-start gap-1.5">{reference}</span>
            ) : (
              <a
                href={link.href}
                target="_blank"
                rel="noreferrer noopener"
                title={
                  link.pinned
                    ? `Opens the file on ${link.host} at the analysed commit`
                    : `Opens the file on ${link.host}, on the default branch as it is today`
                }
                className="inline-flex items-start gap-1.5 rounded-sm hover:text-foreground"
              >
                {reference}
                <ArrowUpRightIcon aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
                <span className="sr-only">(opens on {link.host} in a new tab)</span>
              </a>
            )}
          </li>
        );
      })}
    </ul>
  );
}
