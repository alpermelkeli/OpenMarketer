import type { ReactNode } from "react";

import type { Tone } from "@/lib/analysis/run-status";
import { cn } from "@/lib/utils";

const TONE: Record<Tone, string> = {
  neutral: "border-border bg-surface text-muted-foreground",
  progress: "border-brand/30 bg-brand/5 text-foreground",
  success: "border-success/35 bg-success/8 text-foreground",
  warning: "border-warning/40 bg-warning/8 text-foreground",
  danger: "border-destructive/40 bg-destructive/8 text-foreground",
};

type NoticeProps = {
  tone?: Tone;
  title?: ReactNode;
  children: ReactNode;
  /** `alert` interrupts a screen reader; use it for an error the person just caused. */
  role?: "status" | "alert" | "note";
  className?: string;
};

/** A quiet bordered message: an explanation, a warning or an error, said in place. */
export function Notice({ tone = "neutral", title, children, role = "note", className }: NoticeProps) {
  return (
    <div role={role} className={cn("rounded-md border px-4 py-3 text-sm", TONE[tone], className)}>
      {title && <p className="mb-0.5 text-foreground">{title}</p>}
      <div className="text-pretty text-muted-foreground [&_code]:font-mono [&_code]:text-xs [&_code]:text-foreground">
        {children}
      </div>
    </div>
  );
}
