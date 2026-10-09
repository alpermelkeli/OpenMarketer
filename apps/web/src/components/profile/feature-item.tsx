import { StatusPill } from "@/components/common/status-pill";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { Feature, FeatureStatus } from "@/lib/api/types";
import { featureAnchor } from "@/lib/profile/attention";
import type { EvidenceSource } from "@/lib/profile/evidence-link";
import { FEATURE_STATUSES, featureStatusDisplay, humanizeSlug } from "@/lib/profile/labels";

import { ConfidenceMeter } from "./confidence-meter";
import { EvidenceList } from "./evidence-list";
import { OptionSelect } from "./option-select";

const STATUS_OPTIONS = FEATURE_STATUSES.map((status) => ({
  value: status,
  label: featureStatusDisplay(status).label,
}));

type FeatureItemProps = {
  feature: Feature;
  source: EvidenceSource;
  editing: boolean;
  onChange: (change: Partial<Pick<Feature, "description" | "status">>) => void;
};

/** One feature: what it is, whether it is released, how sure the analyzer was and why. */
export function FeatureItem({ feature, source, editing, onChange }: FeatureItemProps) {
  const status = featureStatusDisplay(feature.status);
  const anchor = featureAnchor(feature.id);
  const descriptionId = `${anchor}-description`;

  return (
    <li id={anchor} className="grid gap-3 py-6 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
        <div className="min-w-0">
          <h4 className="text-lg break-words">{humanizeSlug(feature.id)}</h4>
          <p className="font-mono text-2xs break-all text-muted-foreground">{feature.id}</p>
        </div>
        <div className="flex flex-col items-end gap-2">
          {editing ? (
            <OptionSelect<FeatureStatus>
              label={`Status of ${feature.id}`}
              value={feature.status}
              options={STATUS_OPTIONS}
              onChange={(next) => onChange({ status: next })}
            />
          ) : (
            <StatusPill tone={status.tone}>{status.label}</StatusPill>
          )}
          <ConfidenceMeter confidence={feature.confidence} />
        </div>
      </div>

      {editing ? (
        <div className="grid gap-2">
          <Label htmlFor={descriptionId}>Description</Label>
          <Textarea
            id={descriptionId}
            value={feature.description}
            onChange={(event) => onChange({ description: event.target.value })}
            className="max-w-prose"
          />
        </div>
      ) : (
        <p className="max-w-prose text-pretty break-words whitespace-pre-wrap">{feature.description}</p>
      )}

      <p className="text-xs text-muted-foreground">{status.meaning}</p>
      <EvidenceList evidence={feature.evidence ?? []} source={source} />
    </li>
  );
}
