/**
 * How a research run's progress reads in its chat: a sentence for each stage it recorded, and its
 * state, from its events and, once it has been read, from the run itself.
 *
 * The run's own record wins over its events when it has finished: a run whose workflow could not be
 * started is recorded as failed without ever recording an event. Every sentence is plain text; a
 * query, a title or an address in it is what a model or a web page wrote, which the screen shows as
 * text, never as markup and never as a link.
 *
 * Display only.
 */

import type { Tone } from "@/lib/analysis/run-status";
import type { ResearchRun } from "@/lib/api/types";

import type { ResearchProposal, ResearchRunView, ResearchStage } from "./events";

export type ProgressStatus = {
  state: "running" | "finished" | "failed";
  findings: number | null;
  reason: string | null;
};

export function progressStatus(view: ResearchRunView, run: ResearchRun | undefined): ProgressStatus {
  if (run?.status === "failed") {
    return { state: "failed", findings: view.findings, reason: run.error ?? view.reason };
  }
  if (run?.status === "succeeded" || view.status === "finished") {
    return { state: "finished", findings: view.findings, reason: null };
  }
  return { state: view.status, findings: view.findings, reason: view.reason };
}

const STATE_DISPLAY: Record<ProgressStatus["state"], { label: string; tone: Tone }> = {
  running: { label: "Researching", tone: "progress" },
  finished: { label: "Finished", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
};

export function progressDisplay(state: ProgressStatus["state"]): { label: string; tone: Tone } {
  return STATE_DISPLAY[state];
}

/** The sentence that ends a finished run's card. */
export function outcomeText(status: ProgressStatus): string | null {
  if (status.state !== "finished") return null;
  if (status.findings === null) return "Finished. The report has what it found.";
  return status.findings === 0
    ? "Finished without a checked finding."
    : `Finished with ${count(status.findings, "checked finding", "checked findings")}.`;
}

/** Where the action the policy gate held stands, as a sentence of the run's card. */
export function proposalText(proposal: ResearchProposal): string {
  switch (proposal.status) {
    case "waiting":
      return `The run asked to run ${proposal.actionName} and waits for your decision. Nothing runs until you decide.`;
    case "approved":
      return `You approved ${proposal.actionName}; the run carries it out.`;
    case "rejected":
      return `You rejected ${proposal.actionName}; it did not run.`;
    case "expired":
      return `Nobody decided on ${proposal.actionName} before its deadline, or the run stopped waiting; it did not run.`;
  }
}

/** What one stage did, as a line of the run's card. */
export function stageText(stage: ResearchStage): string {
  switch (stage.kind) {
    case "planned": {
      const queries = count(stage.queries.length, "query", "queries");
      return stage.round === null || stage.round <= 1 ? `Planned ${queries}` : `Planned ${queries} for round ${stage.round}`;
    }
    case "searched":
      return stage.ok
        ? `Searched “${stage.query}”: ${count(stage.results, "result", "results")}`
        : `Searched “${stage.query}”: the search did not answer`;
    case "screened":
      return `Kept ${stage.kept} of ${stage.kept + stage.dropped} results as bearing on the request`;
    case "fetched":
      return `Fetched ${stage.title.trim() === "" ? pageHost(stage.url) : stage.title}`;
    case "fetch_failed":
      return stage.reason === ""
        ? `Could not fetch a page of ${pageHost(stage.url)}`
        : `Could not fetch a page of ${pageHost(stage.url)}: ${stage.reason}`;
    case "read":
      return stage.ok ? `Read ${count(stage.pages, "page", "pages")} so far` : "Could not read a page";
    case "synthesized":
      return `${count(stage.findings, "checked finding", "checked findings")} so far`;
  }
}

/** The host a page's address names, for a line that cannot show its title; the address as it came when it has none. */
export function pageHost(url: string): string {
  try {
    const host = new URL(url).host;
    return host === "" ? url : host;
  } catch {
    return url;
  }
}

function count(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}
