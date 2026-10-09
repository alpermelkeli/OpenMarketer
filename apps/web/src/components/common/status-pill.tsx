import type { ReactNode } from "react";

import type { Tone } from "@/lib/analysis/run-status";
import { cn } from "@/lib/utils";

const TONE: Record<Tone, string> = {
  neutral: "border-border-strong text-muted-foreground",
  progress: "border-brand/40 text-brand",
  success: "border-success/45 text-success",
  warning: "border-warning/50 text-warning",
  danger: "border-destructive/50 text-destructive",
};

type StatusPillProps = {
  tone: Tone;
  children: ReactNode;
  /** A dot that breathes, for something still in progress. */
  live?: boolean;
  className?: string;
};

/** A state said in a word, with a dot in the state's colour. The word carries the meaning. */
export function StatusPill({ tone, children, live = false, className }: StatusPillProps) {
  return (
    <span
      className={cn(
        "inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full border px-2.5 text-xs whitespace-nowrap",
        TONE[tone],
        className,
      )}
    >
      <span aria-hidden="true" className={cn("size-1.5 rounded-full bg-current", live && "animate-pulse-soft")} />
      {children}
    </span>
  );
}
