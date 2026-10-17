import { ArrowUpRightIcon } from "lucide-react";

import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import { ConfidenceMeter } from "@/components/profile/confidence-meter";
import type { ResearchReport } from "@/lib/api/types";
import { httpsUrlOrNull } from "@/lib/profile/evidence-link";
import { findingsOf, questionOf, type Finding, type FindingSource } from "@/lib/research/report";

type ResearchReportViewProps = { report: ResearchReport };

/** A finished run's report: its questions, its findings with their sources, and what it cost. */
export function ResearchReportView({ report }: ResearchReportViewProps) {
  const findings = findingsOf(report);
  return (
    <section aria-labelledby="report" className="grid gap-6 border-t pt-8">
      <h2 id="report" className="font-heading text-lg">
        The report
      </h2>

      <div className="grid gap-2">
        <h3 className="font-heading text-base">Questions</h3>
        <ol className="grid max-w-prose list-decimal gap-1 pl-5 text-sm text-muted-foreground">
          {report.questions.map((question, index) => (
            <li key={index} className="text-pretty">
              {question}
            </li>
          ))}
        </ol>
      </div>

      <div className="grid gap-3">
        <h3 className="font-heading text-base">Findings</h3>
        {findings.length === 0 ? (
          <p className="max-w-prose text-sm text-pretty text-muted-foreground">
            No finding survived the check against the pages the run fetched.
          </p>
        ) : (
          <ul className="grid gap-3">
            {findings.map((finding, index) => (
              <FindingItem key={index} finding={finding} questions={report.questions} />
            ))}
          </ul>
        )}
      </div>

      {report.notes.length > 0 && (
        <Notice title="What the run dropped or worked around">
          <ul className="grid list-disc gap-1 pl-5">
            {report.notes.map((note, index) => (
              <li key={index} className="text-pretty">
                {note}
              </li>
            ))}
          </ul>
        </Notice>
      )}

      <p className="text-xs text-muted-foreground">
        Every finding was checked by code against the pages this run fetched · cost ${report.cost_usd.toFixed(4)} ·
        model <span className="font-mono">{report.model}</span>
      </p>
    </section>
  );
}

function FindingItem({ finding, questions }: { finding: Finding; questions: readonly string[] }) {
  const question = questionOf(finding, questions);
  return (
    <li className="grid gap-3 rounded-md border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <p className="max-w-prose text-pretty">{finding.statement}</p>
        <div className="flex shrink-0 items-center gap-3">
          {finding.flagged && <StatusPill tone="warning">Flagged pages</StatusPill>}
          <ConfidenceMeter confidence={finding.confidence} />
        </div>
      </div>
      {question !== null && <p className="text-xs text-muted-foreground">Answers: {question}</p>}
      {finding.flagged && (
        <p className="max-w-prose text-xs text-pretty text-warning">
          Every page this finding rests on showed signs of an attempt to instruct a model. The finding may still be
          right, but treat it with care.
        </p>
      )}
      <FindingSources sources={finding.sources} />
    </li>
  );
}

function FindingSources({ sources }: { sources: readonly FindingSource[] }) {
  if (sources.length === 0) {
    return <p className="text-xs text-warning">No page is recorded for this finding.</p>;
  }
  return (
    <ul aria-label="Sources" className="grid gap-4">
      {sources.map((source, index) => (
        <SourceItem key={`${source.url}:${index}`} source={source} />
      ))}
    </ul>
  );
}

function SourceItem({ source }: { source: FindingSource }) {
  const href = httpsUrlOrNull(source.url);
  return (
    <li className="grid gap-1.5 text-sm">
      {href === null ? (
        <span className="break-all font-mono text-xs text-muted-foreground">{source.url}</span>
      ) : (
        <a
          href={href}
          target="_blank"
          rel="noreferrer noopener"
          className="inline-flex w-fit items-start gap-1.5 rounded-sm text-brand underline-offset-4 hover:underline"
        >
          <span className="break-all font-mono text-xs">{href}</span>
          <ArrowUpRightIcon aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
          <span className="sr-only">(opens in a new tab)</span>
        </a>
      )}
      {source.quotes.map((quote, index) => (
        <blockquote key={index} className="border-l-2 pl-3 text-pretty text-muted-foreground">
          “{quote}”
        </blockquote>
      ))}
    </li>
  );
}
