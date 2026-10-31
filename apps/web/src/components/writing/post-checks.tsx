import { Verdict } from "@/components/common/verdict";
import type { CheckReport, Review } from "@/lib/writing/record";

/**
 * What the code's rules found of a text: each rule it broke, with what the check found, or that it
 * kept every one. Only what the run recorded is said; a rule it did not report on is not claimed.
 */
export function CodeChecks({ report }: { report: CheckReport | null }) {
  return (
    <div className="grid gap-1.5">
      <p className="text-2xs tracking-widest text-muted-foreground uppercase">Checks by code</p>
      {report === null ? (
        <p className="text-xs text-muted-foreground">No report of the code&rsquo;s checks was stored.</p>
      ) : (
        <ul className="grid gap-1.5">
          {report.passed === true && report.failures.length === 0 && (
            <li>
              <Verdict passed label="Every rule of the code that applies to this project" />
            </li>
          )}
          {report.passed === null && report.failures.length === 0 && (
            <li>
              <Verdict passed={false} unconfirmed label="Every rule of the code">
                the report does not say whether the text kept them
              </Verdict>
            </li>
          )}
          {report.failures.map((failure, index) => (
            <li key={`${failure.rule}-${index}`}>
              {/* The finding is the check's sentence, which may quote the text: shown as text. */}
              <Verdict passed={false} label={failure.label}>
                {failure.finding}
              </Verdict>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * The critic's review of a text, criterion by criterion, with its notes, whether it found a risky
 * claim and what it suggested. The critic is a model: every note is shown as text only.
 */
export function CriticReview({ review, missing }: { review: Review | null; missing: string }) {
  return (
    <div className="grid gap-1.5">
      <p className="text-2xs tracking-widest text-muted-foreground uppercase">The critic</p>
      {review === null ? (
        <p className="text-xs text-muted-foreground">{missing}</p>
      ) : (
        <>
          <ul className="grid gap-1.5">
            {review.criteria.map((criterion, index) => (
              <li key={`${criterion.label}-${index}`} className="grid gap-0.5">
                <Verdict passed={criterion.passed} label={criterion.label} minor={criterion.minor}>
                  {criterion.severity !== "" && criterion.severity !== "No issue" && criterion.severity}
                </Verdict>
                {criterion.note !== "" && (
                  <p className="max-w-prose pl-6 text-xs text-pretty whitespace-pre-wrap text-muted-foreground">
                    {criterion.note}
                  </p>
                )}
              </li>
            ))}
            <li className="grid gap-0.5">
              {review.riskyClaim === null ? (
                <Verdict passed={false} unconfirmed label="No risky claim">
                  the critic did not say
                </Verdict>
              ) : (
                <Verdict passed={!review.riskyClaim} label="No risky claim" />
              )}
              {review.riskyNote !== "" && (
                <p className="max-w-prose pl-6 text-xs text-pretty whitespace-pre-wrap text-muted-foreground">
                  {review.riskyNote}
                </p>
              )}
            </li>
          </ul>
          {review.suggestions.length > 0 && (
            <div className="grid gap-1 pt-1">
              <p className="text-xs text-muted-foreground">What it suggested changing</p>
              <ul className="grid max-w-prose list-disc gap-0.5 pl-5 text-xs">
                {review.suggestions.map((suggestion, index) => (
                  <li key={index} className="text-pretty whitespace-pre-wrap">
                    {suggestion}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** Numbers in a text that the profile does not hold: not refused, but for a person to check. */
export function FlaggedNumbers({ numbers }: { numbers: readonly string[] }) {
  if (numbers.length === 0) return null;
  return (
    <p className="text-xs text-pretty text-warning">
      Numbers the profile does not hold, to check before approving:{" "}
      {numbers.map((number, index) => (
        <span key={`${number}-${index}`}>
          {index > 0 && ", "}
          <span className="font-mono tabular-nums text-foreground">{number}</span>
        </span>
      ))}
    </p>
  );
}
