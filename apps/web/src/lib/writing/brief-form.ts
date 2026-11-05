/**
 * The form that asks the writer for a post, as it holds what a person chose and typed and as the
 * API takes it. The goal decides which field is sent: an announcement names a feature, an angle
 * carries the person's words, an introduction carries neither. Every rule of a valid brief (how
 * long an angle may be, whether the feature is live, whether an image or a research run may still
 * be used, whether the image was made for the post's platform) is the API's and the run's; their
 * answer is put beside the field. Which images and runs are offered, and that an Instagram post asks
 * for an image, are display choices only: the API checks again.
 */

import type { ApiError } from "@/lib/api/api-error";
import type {
  Feature,
  MediaAsset,
  ProductProfile,
  ResearchRun,
  WritingBrief,
  WritingGoal,
  WritingPlatform,
} from "@/lib/api/types";
import { formProblems, type FormProblems } from "@/lib/settings/form-problems";

export type BriefForm = {
  platform: WritingPlatform;
  goal: WritingGoal;
  /** The feature an announcement names; "" while none is chosen. */
  feature: string;
  angle: string;
  variants: number;
  /** The Studio asset whose image the post goes with; "" for a post alone. */
  asset: string;
  /** The research run whose findings the writer may draw on; "" for none. */
  researchRunId: string;
};

export const DEFAULT_VARIANTS = 3;

/** How many texts a person may ask for, as the API takes them: one to five. */
export const VARIANT_OPTIONS: readonly number[] = [1, 2, 3, 4, 5];

export const EMPTY_BRIEF: BriefForm = {
  platform: "x",
  goal: "introduce",
  feature: "",
  angle: "",
  variants: DEFAULT_VARIANTS,
  asset: "",
  researchRunId: "",
};

/**
 * The brief as the API takes it: only the field of the chosen goal, the angle trimmed, and the
 * image and the research run only when one was chosen.
 */
export function writingBriefOf(form: BriefForm): WritingBrief {
  const brief: WritingBrief = {
    platform: form.platform,
    goal: form.goal,
    variants: form.variants,
  };
  if (form.goal === "announce") brief.feature = form.feature === "" ? null : form.feature;
  if (form.goal === "angle") brief.angle = form.angle.trim();
  if (form.asset !== "") brief.asset = form.asset;
  if (form.researchRunId !== "") brief.research_run_id = form.researchRunId;
  return brief;
}

/**
 * The library's images a post can go with: those not rejected that have an image already. A draft
 * is offered too, since approving the post approves its image with it.
 */
export function attachableAssets(assets: readonly MediaAsset[]): MediaAsset[] {
  return assets.filter((asset) => asset.status !== "rejected" && asset.current_version_id !== null);
}

// The Studio platforms whose images a post on each platform goes with, as the writer's rule has
// them; null for any. An Instagram post is an image with its caption, made for Instagram.
const POST_IMAGES: Record<WritingPlatform, readonly MediaAsset["platform"][] | null> = {
  x: null,
  instagram: ["instagram_post", "instagram_portrait", "instagram_story"],
};

/** Whether a post on `platform` cannot be text alone: its brief must name an image. */
export function needsImage(platform: WritingPlatform): boolean {
  return POST_IMAGES[platform] !== null;
}

/** Whether a post on `platform` can go with `asset`, by the platform the image was made for. */
export function imageFitsPost(platform: WritingPlatform, asset: MediaAsset): boolean {
  const fitting = POST_IMAGES[platform];
  return fitting === null || fitting.includes(asset.platform);
}

/** What the image choice offers a post on `platform`: the images made for it among `offered`. */
export function imagesForPost(offered: Offered<MediaAsset>, platform: WritingPlatform): Offered<MediaAsset> {
  if (offered.state !== "known") return offered;
  return {
    state: "known",
    items: offered.items.filter((asset) => imageFitsPost(platform, asset)),
  };
}

/** The research runs whose findings a post can draw on: those that succeeded, with a report. */
export function usableResearchRuns(runs: readonly ResearchRun[]): ResearchRun[] {
  return runs.filter((run) => run.status === "succeeded");
}

/** What a choice of the form can offer: nothing yet, nothing because the list failed, or these. */
export type Offered<T> = { state: "reading" } | { state: "unreadable" } | { state: "known"; items: readonly T[] };

/** The list a choice offers, from what its query holds: `usable` picks the items it may offer. */
export function offeredOf<T>(
  listed: readonly T[] | undefined,
  failed: boolean,
  usable: (items: readonly T[]) => T[],
): Offered<T> {
  if (listed !== undefined) return { state: "known", items: usable(listed) };
  return failed ? { state: "unreadable" } : { state: "reading" };
}

/**
 * The features an announcement can name: those the approved profile holds as live, the only ones
 * that may be mentioned publicly. Whether one still is when the run starts is the run's to check.
 */
export function announceableFeatures(profile: ProductProfile | null): Feature[] {
  return profile === null ? [] : (profile.features ?? []).filter((feature) => feature.status === "live");
}

const FIELDS = ["goal", "feature", "angle", "variants", "platform", "asset", "research_run_id"] as const;

type Field = (typeof FIELDS)[number];

// A refusal of the chosen image or research run is about that field, though it is not a 422.
const CHOICE_REFUSALS: Partial<Record<ApiError["code"], { field: Field; say: (error: ApiError) => string }>> = {
  media_asset_not_found: {
    field: "asset",
    say: () => "The project's library no longer has this image. Choose another, or none.",
  },
  media_asset_not_usable: {
    field: "asset",
    // The API's message carries the reason (rejected, still being made, without an image).
    say: (error) => `This image cannot go with a post. The API says: ${error.message}`,
  },
  research_not_found: {
    field: "research_run_id",
    say: () => "The project no longer has this research run. Choose another, or none.",
  },
  research_not_usable: {
    field: "research_run_id",
    say: () => "This research run has not succeeded, so it has no findings to draw on. Choose another, or none.",
  },
};

/** The API's refusal, beside the field it is about; one about the brief as a whole is the form's. */
export function briefProblems(error: ApiError | null): FormProblems {
  const refusal = error === null ? undefined : CHOICE_REFUSALS[error.code];
  if (error !== null && refusal !== undefined) return { fields: { [refusal.field]: [refusal.say(error)] }, form: [] };
  return formProblems(error, FIELDS);
}
