/**
 * What each role and tier of the model configuration is called and what it is for,
 * where its model comes from, and what a problem the check found means. Display only:
 * which roles and tiers exist is the API's to say.
 *
 * Sentences are the dashboard's own wording, with names between backticks.
 */

import type { Tone } from "@/lib/analysis/run-status";
import type { CapabilityProblem, RoleModelSource, TierModelSource } from "@/lib/api/types";

export type RoleLabel = {
  label: string;
  purpose: string;
  /** Whether anything that runs today makes calls with the role. */
  inUse: boolean;
};

// A map, not an object: a name such as "constructor" must not find something inherited.
const ROLES = new Map<string, RoleLabel>([
  [
    "repo_analyzer",
    {
      label: "Repository analyzer",
      purpose: "Explores a repository with read-only tools and drafts its Product Profile. Every analysis uses it.",
      inUse: true,
    },
  ],
  ["orchestrator", { label: "Orchestrator", purpose: "Answers questions and takes requests in the dashboard's chat.", inUse: false }],
  [
    "researcher",
    {
      label: "Researcher",
      purpose: "Plans the searches of a research run and writes its findings from what the reader read. Every research run uses it.",
      inUse: true,
    },
  ],
  [
    "screener",
    {
      label: "Screener",
      purpose:
        "Drops search results that do not bear on a research request before any page is fetched. It answers with probabilities only and takes a model only.",
      inUse: true,
    },
  ],
  [
    "reader",
    {
      label: "Reader",
      purpose:
        "Reads untrusted text, such as the pages a research run fetched, into fixed fields. It is never given tools.",
      inUse: true,
    },
  ],
  ["planner", { label: "Planner", purpose: "Writes the weekly plan, the channel mix and the ad plan.", inUse: false }],
  ["writer", { label: "Writer", purpose: "Writes posts, captions, threads and ad copy in the brand's voice.", inUse: false }],
  ["reply_drafter", { label: "Reply drafter", purpose: "Drafts replies to the project's own audience.", inUse: false }],
  [
    "creative_director",
    { label: "Creative director", purpose: "Writes the prompts and scripts for image, video and voice.", inUse: false },
  ],
  [
    "studio",
    {
      label: "Creative studio",
      purpose: "Produces images, video and voice, looks at the results and edits them. It must accept images.",
      inUse: false,
    },
  ],
  [
    "asset_verifier",
    {
      label: "Asset verifier",
      purpose: "Checks every produced asset against its brief. Keep it a different model from the studio's.",
      inUse: false,
    },
  ],
  ["critic", { label: "Critic", purpose: "Checks a draft against the brand voice and the facts before approval.", inUse: false }],
  ["analyst", { label: "Analyst", purpose: "Reads the metrics and writes the weekly report and digests.", inUse: false }],
  ["vision", { label: "Vision", purpose: "Understands screenshots and creative assets.", inUse: false }],
  [
    "judge",
    {
      label: "Judge",
      purpose:
        "Scores the analyzer in the benchmark. The benchmark runs from the command line and reads the environment only, so a choice here does not reach it.",
      inUse: false,
    },
  ],
  [
    "image_generator",
    {
      label: "Image generator",
      purpose: "Makes and edits the studio's images, the text in them included. It takes a model only, one that answers with images.",
      inUse: false,
    },
  ],
  ["embeddings", { label: "Embeddings", purpose: "Turns text into vectors for the memory. It takes a model only.", inUse: false }],
]);

/** The words for one role. A role the configuration added after this was written is called by its name. */
export function roleLabel(role: string): RoleLabel {
  return ROLES.get(role) ?? { label: role, purpose: "This dashboard has no description of this role yet.", inUse: false };
}

const TIERS = new Map<string, string>([
  ["fast", "Cheap and quick: classification, short replies, and by design the repository analyzer."],
  ["balanced", "The default workhorse: writing and checking."],
  ["strong", "Hard reasoning: planning and strategy."],
]);

export function tierPurpose(tier: string): string {
  return TIERS.get(tier) ?? "A class of models the configuration names; every role on it follows its model.";
}

type SourceDisplay = { label: string; tone: Tone };

const TIER_SOURCES: Record<TierModelSource, SourceDisplay> = {
  environment: { label: "Set by the environment", tone: "warning" },
  workspace: { label: "Chosen here", tone: "success" },
  file: { label: "From the configuration file", tone: "neutral" },
};

export function tierSourceDisplay(source: TierModelSource): SourceDisplay {
  return TIER_SOURCES[source];
}

/** Where a role's model comes from; one that follows its tier names the tier. */
export function roleSourceDisplay(source: RoleModelSource, tier: string | null): SourceDisplay {
  switch (source) {
    case "environment":
      return { label: "Set by the environment", tone: "warning" };
    case "workspace":
      return { label: "Chosen here", tone: "success" };
    case "file":
      return { label: "From the configuration file", tone: "neutral" };
    case "tier":
      return { label: tier === null ? "Follows its tier" : `Follows the ${tier} tier`, tone: "neutral" };
    case "default":
      return { label: "The configuration's default", tone: "neutral" };
  }
}

const PROBLEMS: Record<CapabilityProblem, string> = {
  not_in_catalog: "is not in the provider's catalogue. Check its name.",
  no_tools: "cannot call tools, which this role needs.",
  no_structured_output: "cannot answer in a set structure, which this role needs.",
  no_image_input: "does not accept images, which this role needs.",
  no_image_output: "does not answer with images, which this role makes.",
  rejects_temperature:
    "does not accept a temperature, which this role's requests carry. Mark it below as rejecting the temperature.",
  fallback_rejects_temperature:
    "is a fallback that does not accept a temperature. A fallback is sent the request made for the first model, temperature included, so the provider may pass it over. Choose another fallback, or a first model that is sent no temperature.",
};

/** What a problem the check found means, to follow the model's name. */
export function problemText(problem: CapabilityProblem): string {
  return PROBLEMS[problem];
}
