import { CheckIcon, MinusIcon, XIcon } from "lucide-react";
import type { ReactNode } from "react";

import type { MediaAssetVersion } from "@/lib/api/types";
import { checkReportOf, verifierReportOf } from "@/lib/studio/reports";
import { cn } from "@/lib/utils";

type VersionReviewsProps = { version: MediaAssetVersion };

/**
 * What the checks and the verifier found of one version, criterion by criterion: each says in a
 * word whether it held and has a shape for it. Everything in the reports was written by a run,
 * the verifier's notes by a model: it is shown as text only.
 */
export function VersionReviews({ version }: VersionReviewsProps) {
  const check = checkReportOf(version.check_report);
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
              <Verdict passed={item.passed} label={item.label}>
                {item.detail}
              </Verdict>
              {item.lines !== undefined && (
                <ul aria-label="Lines that do not read as asked" className="grid gap-1 pl-6 text-xs">
                  {item.lines.map((line, index) => (
                    <li key={index} className="text-pretty">
                      <span className="text-muted-foreground">Should read </span>“{line.expected}”
                      <span className="text-muted-foreground">
                        {line.read === null ? ", not found in the image" : ", reads "}
                      </span>
                      {line.read !== null && <>“{line.read}”</>}
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

type VerdictProps = { passed: boolean; label: string; minor?: boolean; children?: ReactNode };

/** "Passes" or "Fails", with a tick, a cross or a dash for a minor note, then what was found. */
function Verdict({ passed, label, minor = false, children }: VerdictProps) {
  const Icon = passed ? (minor ? MinusIcon : CheckIcon) : XIcon;
  return (
    <p className="flex flex-wrap items-baseline gap-x-2 text-sm">
      <span
        className={cn(
          "inline-flex size-4 shrink-0 translate-y-0.5 items-center justify-center rounded-full border",
          passed ? (minor ? "border-warning/60 text-warning" : "border-success/50 text-success") : "border-destructive/60 text-destructive",
        )}
      >
        <Icon aria-hidden="true" className="size-3" />
      </span>
      <span>{label}</span>
      <span className={passed ? "text-xs text-muted-foreground" : "text-xs text-destructive"}>
        {passed ? (minor ? "Passes, with a note" : "Passes") : "Fails"}
      </span>
      {children !== undefined && children !== false && children !== "" && (
        <span className="text-xs text-muted-foreground">· {children}</span>
      )}
    </p>
  );
}
