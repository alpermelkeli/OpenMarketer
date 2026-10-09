import { confidenceLevel, confidencePercent, type ConfidenceLevel } from "@/lib/profile/confidence";
import { cn } from "@/lib/utils";

const LEVEL: Record<ConfidenceLevel, { label: string; bar: string; text: string }> = {
  low: { label: "Low confidence", bar: "bg-warning", text: "text-warning" },
  medium: { label: "Medium confidence", bar: "bg-foreground/55", text: "text-muted-foreground" },
  high: { label: "High confidence", bar: "bg-foreground/55", text: "text-muted-foreground" },
};

type ConfidenceMeterProps = {
  /** 0 to 1, as the analyzer stated it. */
  confidence: number;
};

/** How sure the analyzer said it was: a thin bar, the percentage and the band in words. */
export function ConfidenceMeter({ confidence }: ConfidenceMeterProps) {
  const percent = confidencePercent(confidence);
  const level = LEVEL[confidenceLevel(confidence)];
  return (
    <div className="flex items-center gap-2 text-xs whitespace-nowrap">
      <div
        role="meter"
        aria-label="Confidence"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-valuetext={`${percent}%, ${level.label.toLowerCase()}`}
        className="h-1 w-12 overflow-hidden rounded-full bg-border"
      >
        <div className={cn("h-full rounded-full", level.bar)} style={{ width: `${percent}%` }} />
      </div>
      <span className={cn("tabular-nums", level.text)}>
        {percent}% · {level.label}
      </span>
    </div>
  );
}
