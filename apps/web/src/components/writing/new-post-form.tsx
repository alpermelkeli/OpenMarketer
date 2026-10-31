"use client";

import { useId, useState, type FormEvent } from "react";

import { FieldErrors } from "@/components/common/field-errors";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { Feature, MediaAsset, ResearchRun, WritingBrief, WritingGoal } from "@/lib/api/types";
import { fieldProblems, type FormProblems } from "@/lib/settings/form-problems";
import {
  EMPTY_BRIEF,
  VARIANT_OPTIONS,
  writingBriefOf,
  type BriefForm,
  type Offered,
} from "@/lib/writing/brief-form";
import { GOAL_OPTIONS } from "@/lib/writing/status";

import { PostImageChoice } from "./post-image-choice";
import { PostResearchChoice } from "./post-research-choice";

type MoreOf = { hasMore: boolean; loading: boolean; onLoadMore: () => void };

type NewPostFormProps = {
  projectId: string;
  /** The live features of the approved profile, which an announcement can name; null while read. */
  features: readonly Feature[] | null;
  /** The library's images a post can go with. */
  assets: Offered<MediaAsset>;
  moreAssets: MoreOf;
  /** The project's research runs that succeeded. */
  researchRuns: Offered<ResearchRun>;
  moreResearchRuns: MoreOf;
  problems: FormProblems;
  pending: boolean;
  onSubmit: (brief: WritingBrief) => void;
};

const VARIANT_ITEMS = VARIANT_OPTIONS.map((n) => ({ value: String(n), label: String(n) }));

/**
 * What the writer is asked for: a goal, the feature or the angle it needs, how many texts, and, if
 * a person wants them, an image of the library for the post to go with and research to draw on.
 */
export function NewPostForm({
  projectId,
  features,
  assets,
  moreAssets,
  researchRuns,
  moreResearchRuns,
  problems,
  pending,
  onSubmit,
}: NewPostFormProps) {
  const id = useId();
  const [form, setForm] = useState<BriefForm>(EMPTY_BRIEF);
  const goalProblems = fieldProblems(problems, "goal");
  const featureProblems = fieldProblems(problems, "feature");
  const angleProblems = fieldProblems(problems, "angle");
  const variantProblems = fieldProblems(problems, "variants");
  const platformProblems = fieldProblems(problems, "platform");
  const featureItems = (features ?? []).map((feature) => ({ value: feature.id, label: feature.id }));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    onSubmit(writingBriefOf(form));
  }

  return (
    <form onSubmit={submit} className="grid max-w-prose gap-5" autoComplete="off" noValidate>
      {/* One platform only, so there is nothing to choose: it is said rather than offered. */}
      <div className="grid gap-2">
        <p className="text-sm">
          For X <span className="text-muted-foreground">· the only platform the writer writes for so far</span>
        </p>
        <FieldErrors id={`${id}-platform-errors`} messages={platformProblems} />
      </div>

      <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_8rem]">
        <div className="grid gap-2">
          <Label id={`${id}-goal-label`}>What the post is for</Label>
          <Select
            items={GOAL_OPTIONS}
            value={form.goal}
            onValueChange={(next) => {
              if (next !== null) setForm({ ...form, goal: next as WritingGoal });
            }}
            disabled={pending}
          >
            <SelectTrigger
              aria-labelledby={`${id}-goal-label`}
              aria-invalid={goalProblems.length > 0}
              aria-describedby={`${id}-goal-errors`}
              className="w-full"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {GOAL_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FieldErrors id={`${id}-goal-errors`} messages={goalProblems} />
        </div>
        <div className="grid gap-2">
          <Label id={`${id}-variants-label`}>Texts to choose from</Label>
          <Select
            items={VARIANT_ITEMS}
            value={String(form.variants)}
            onValueChange={(next) => {
              if (next !== null) setForm({ ...form, variants: Number(next) });
            }}
            disabled={pending}
          >
            <SelectTrigger
              aria-labelledby={`${id}-variants-label`}
              aria-invalid={variantProblems.length > 0}
              aria-describedby={`${id}-variants-errors`}
              className="w-full"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {VARIANT_ITEMS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FieldErrors id={`${id}-variants-errors`} messages={variantProblems} />
        </div>
      </div>

      {form.goal === "announce" && (
        <div className="grid gap-2">
          <Label id={`${id}-feature-label`}>The live feature to announce</Label>
          <Select
            items={featureItems}
            value={form.feature === "" ? null : form.feature}
            onValueChange={(next) => setForm({ ...form, feature: next ?? "" })}
            disabled={pending || featureItems.length === 0}
          >
            <SelectTrigger
              aria-labelledby={`${id}-feature-label`}
              aria-invalid={featureProblems.length > 0}
              aria-describedby={`${id}-feature-hint ${id}-feature-errors`}
              className="w-full font-mono"
            >
              <SelectValue placeholder={features === null ? "Reading the approved profile…" : "Choose a feature"} />
            </SelectTrigger>
            <SelectContent>
              {featureItems.map((option) => (
                <SelectItem key={option.value} value={option.value} className="font-mono">
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p id={`${id}-feature-hint`} className="text-xs text-pretty text-muted-foreground">
            {features !== null && featureItems.length === 0
              ? "The approved profile holds no feature as live, so there is nothing to announce yet."
              : "Only features the approved profile holds as live can be announced. The run checks again when it starts."}
          </p>
          <FieldErrors id={`${id}-feature-errors`} messages={featureProblems} />
        </div>
      )}

      {form.goal === "angle" && (
        <div className="grid gap-2">
          <Label htmlFor={`${id}-angle`}>Your angle</Label>
          <Textarea
            id={`${id}-angle`}
            value={form.angle}
            onChange={(event) => setForm({ ...form, angle: event.target.value })}
            placeholder="Teams that dread the monthly export"
            maxLength={600}
            rows={3}
            disabled={pending}
            aria-invalid={angleProblems.length > 0}
            aria-describedby={`${id}-angle-hint ${id}-angle-errors`}
          />
          <p id={`${id}-angle-hint`} className="text-xs text-pretty text-muted-foreground">
            The idea the post should be built on, in your words. The writer plans from it, and still claims only what
            the approved profile holds as live.
          </p>
          <FieldErrors id={`${id}-angle-errors`} messages={angleProblems} />
        </div>
      )}

      <PostImageChoice
        id={id}
        projectId={projectId}
        assets={assets}
        value={form.asset}
        onChange={(asset) => setForm({ ...form, asset })}
        problems={fieldProblems(problems, "asset")}
        disabled={pending}
        more={moreAssets}
      />

      <PostResearchChoice
        id={id}
        projectId={projectId}
        runs={researchRuns}
        value={form.researchRunId}
        onChange={(researchRunId) => setForm({ ...form, researchRunId })}
        problems={fieldProblems(problems, "research_run_id")}
        disabled={pending}
        more={moreResearchRuns}
      />

      <FieldErrors id={`${id}-form-errors`} messages={problems.form} />
      <div className="grid gap-2">
        <p className="text-sm text-pretty text-muted-foreground">
          The run writes from the approved profile: it plans the post, writes the texts, checks them in code and has
          the critic read them, then asks you in Approvals to approve the best one, with its image when one is chosen.
          Nothing is sent to X: an approved post is recorded as a dry run.
        </p>
        <div>
          <Button type="submit" disabled={pending}>
            {pending ? "Starting…" : "Start the run"}
          </Button>
        </div>
      </div>
    </form>
  );
}
