import { Verdict } from "@/components/common/verdict";
import type { MediaAssetVersion } from "@/lib/api/types";
import { checkReportOf, verifierReportOf } from "@/lib/studio/reports";

type VersionReviewsProps = { version: MediaAssetVersion };

/**
 * What the checks and the verifier found of one version, criterion by criterion: each says in a
 * word whether it held and has a shape for it. Everything in the reports was written by a run,
 * the verifier's notes by a model: it is shown as text only.
 */
export function VersionReviews({ version }: VersionReviewsProps) {
  const check = checkReportOf(version.check_report, version.verifier_report);
  const verifier = verifierReportOf(version.verifier_report);

  if (check === null) {
    return <p className="text-xs text-muted-foreground">Not reviewed: the run did not offer this version as a candidate.</p>;
  }

  return (
    <div className="grid gap-4">
      <div className="grid gap-1.5">
        <p className="text-2xs tracking-widest text-muted-foreground uppercase">Checks by code</p>
        <ul className="grid gap-1.5">
          {check.items.map((item) => (
            <li key={item.label} className="grid gap-1">
              <Verdict passed={item.passed} unconfirmed={item.unconfirmed} label={item.label}>
                {item.detail}
              </Verdict>
              {item.confirmed !== undefined && item.confirmed.length > 0 && (
                <ul aria-label="Lines OCR confirmed" className="grid gap-1 pl-6 text-xs">
                  {item.confirmed.map((line, index) => (
                    <li key={index} className="text-pretty">
                      “{line}”<span className="text-muted-foreground">, read by OCR as asked</span>
                    </li>
                  ))}
                </ul>
              )}
              {item.lines !== undefined && (
                <ul aria-label="Lines OCR could not confirm" className="grid gap-1.5 pl-6 text-xs">
                  {item.lines.map((line, index) => (
                    <li key={index} className="grid gap-0.5 text-pretty">
                      <span>
                        <span className="text-muted-foreground">Should read </span>“{line.expected}”
                        <span className="text-muted-foreground">
                          {line.read === null ? ", OCR read nothing close to it" : ", OCR read "}
                        </span>
                        {line.read !== null && <>“{line.read}”</>}
                      </span>
                      {line.verdict !== null && (
                        <span
                          className={
                            line.verifier === null
                              ? "text-warning"
                              : line.verifier.matches
                                ? "text-muted-foreground"
                                : "text-destructive"
                          }
                        >
                          {line.verdict}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
          {check.other.map((name) => (
            <li key={name}>
              <Verdict passed={false} label="Other">
                <span className="font-mono">{name}</span>
              </Verdict>
            </li>
          ))}
          {check.otherNotices.map((name) => (
            <li key={`notice-${name}`}>
              <Verdict passed={false} unconfirmed label="Noted">
                <span className="font-mono">{name}</span>
              </Verdict>
            </li>
          ))}
        </ul>
        <p className="text-xs text-muted-foreground">
          Code does not measure the margin kept clear of words: the verifier judges it.
        </p>
      </div>

      <div className="grid gap-1.5">
        <p className="text-2xs tracking-widest text-muted-foreground uppercase">The verifier</p>
        {verifier === null ? (
          <p className="text-xs text-muted-foreground">
            Not asked: a version that fails the checks by code is not shown to the verifier.
          </p>
        ) : (
          <>
            <ul className="grid gap-1.5">
              {verifier.criteria.map((criterion) => (
                <li key={criterion.label} className="grid gap-0.5">
                  <Verdict passed={criterion.passed} label={criterion.label} minor={criterion.severity === "Minor"}>
                    {criterion.severity !== "" && criterion.severity !== "No issue" && criterion.severity}
                  </Verdict>
                  {criterion.note !== "" && (
                    <p className="max-w-prose pl-6 text-xs text-pretty whitespace-pre-wrap text-muted-foreground">
                      {criterion.note}
                    </p>
                  )}
                </li>
              ))}
            </ul>
            {verifier.suggestions.length > 0 && (
              <div className="grid gap-1 pt-1">
                <p className="text-xs text-muted-foreground">What it suggested changing</p>
                <ul className="grid max-w-prose list-disc gap-0.5 pl-5 text-xs">
                  {verifier.suggestions.map((suggestion, index) => (
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
    </div>
  );
}
