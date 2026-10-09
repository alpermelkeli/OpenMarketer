import type { ReactNode } from "react";

import type { Evidence } from "@/lib/api/types";
import { sectionAnchor, type SectionKey } from "@/lib/profile/attention";
import type { EvidenceSource } from "@/lib/profile/evidence-link";

import { ConfidenceMeter } from "./confidence-meter";
import { EvidenceList } from "./evidence-list";

type ProfileSectionProps = {
  section: SectionKey;
  title: string;
  /** What this part of the profile is for, in a line. */
  purpose: string;
  /** The section's claim, or nothing when the profile has no such section. */
  claim: { evidence?: Evidence[]; confidence: number } | undefined;
  source: EvidenceSource;
  children: ReactNode;
};

/** One section of the profile: its values, then how sure the analyzer was and why. */
export function ProfileSection({ section, title, purpose, claim, source, children }: ProfileSectionProps) {
  const headingId = `${sectionAnchor(section)}-heading`;
  return (
    <section id={sectionAnchor(section)} aria-labelledby={headingId} className="grid gap-5 border-t pt-8">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        <div>
          <h3 id={headingId} className="font-heading text-title">
            {title}
          </h3>
          <p className="text-sm text-muted-foreground">{purpose}</p>
        </div>
        {claim !== undefined && <ConfidenceMeter confidence={claim.confidence} />}
      </div>
      {claim === undefined ? (
        <p className="text-sm text-muted-foreground">This profile has no such section.</p>
      ) : (
        <>
          <dl className="grid gap-x-8 gap-y-5 sm:grid-cols-[10rem_minmax(0,1fr)]">{children}</dl>
          <EvidenceList evidence={claim.evidence ?? []} source={source} />
        </>
      )}
    </section>
  );
}
