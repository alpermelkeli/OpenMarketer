import { CheckIcon } from "lucide-react";

import type { AttentionItem, AttentionReason } from "@/lib/profile/attention";
import { confidencePercent } from "@/lib/profile/confidence";
import { humanizeSlug } from "@/lib/profile/labels";

type AttentionListProps = {
  items: readonly AttentionItem[];
};

function reasonText(reason: AttentionReason, confidence: number): string {
  switch (reason) {
    case "status_unknown":
      return "Status unknown: is it released?";
    case "no_evidence":
      return "No evidence cited";
    case "low_confidence":
      return `Low confidence (${confidencePercent(confidence)}%)`;
  }
}

/** What the analyzer was unsure about, first on the page: these are the reviewer's decisions. */
export function AttentionList({ items }: AttentionListProps) {
  if (items.length === 0) {
    return (
      <section aria-labelledby="attention-heading" className="flex items-start gap-3 rounded-md border px-5 py-4">
        <CheckIcon aria-hidden="true" className="mt-1 size-4 shrink-0 text-success" />
        <div>
          <h3 id="attention-heading">Nothing is flagged</h3>
          <p className="text-sm text-pretty text-muted-foreground">
            Every part has evidence, a decided status and reasonable confidence. That is the analyzer&rsquo;s view of
            itself; it is still worth reading through.
          </p>
        </div>
      </section>
    );
  }
  return (
    <section aria-labelledby="attention-heading" className="rounded-md border border-warning/45 bg-warning/5">
      <div className="px-5 pt-4 pb-3">
        <h3 id="attention-heading" className="font-heading text-title">
          Needs your attention
          <span className="ml-2 font-sans text-sm text-warning tabular-nums">{items.length}</span>
        </h3>
        <p className="max-w-prose text-sm text-pretty text-muted-foreground">
          The analyzer was unsure here, so these are yours to decide. Most uncertain first.
        </p>
      </div>
      <ul className="divide-y divide-warning/20 border-t border-warning/20">
        {items.map((item) => (
          <li key={item.anchor}>
            <a
              href={`#${item.anchor}`}
              className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-0.5 px-5 py-2.5 transition-colors hover:bg-warning/8 focus-visible:-outline-offset-2"
            >
              <span className="min-w-0 break-words">
                <span className="mr-2 text-2xs tracking-widest text-muted-foreground uppercase">
                  {item.kind === "feature" ? "Feature" : "Section"}
                </span>
                {humanizeSlug(item.subject)}
              </span>
              <span className="text-sm text-warning">
                {item.reasons.map((reason) => reasonText(reason, item.confidence)).join(" · ")}
              </span>
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}
