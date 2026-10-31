import { CheckIcon, MinusIcon, XIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type VerdictProps = {
  passed: boolean;
  label: string;
  minor?: boolean;
  /** Neither passed nor failed: nothing confirmed it. */
  unconfirmed?: boolean;
  children?: ReactNode;
};

/**
 * "Passes", "Fails" or "Not confirmed", with a tick, a cross, or a dash for a minor note or
 * for what nothing confirmed, then what was found. What passed is what a run recorded.
 */
export function Verdict({ passed, label, minor = false, unconfirmed = false, children }: VerdictProps) {
  if (unconfirmed) {
    return (
      <p className="flex flex-wrap items-baseline gap-x-2 text-sm">
        <span className="inline-flex size-4 shrink-0 translate-y-0.5 items-center justify-center rounded-full border border-dashed border-warning/60 text-warning">
          <MinusIcon aria-hidden="true" className="size-3" />
        </span>
        <span>{label}</span>
        <span className="text-xs text-warning">Not confirmed</span>
        {children !== undefined && children !== false && children !== "" && (
          <span className="text-xs text-muted-foreground">· {children}</span>
        )}
      </p>
    );
  }
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
