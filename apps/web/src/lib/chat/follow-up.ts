/**
 * How a follow-up turn is worded on its chat's screen: the turn the orchestrator starts by itself
 * once a run a turn of the chat started has ended, to explain that run in the thread. A turn that
 * answers a message is no follow-up; it is worded as one about a run of any kind, should it come.
 *
 * Display only.
 */

import type { TurnTrigger } from "./events";

type Wording = { reviewing: string; failed: string; stillThere: string };

const WORDING: Record<TurnTrigger, Wording> = {
  research_finished: {
    reviewing: "The orchestrator is reviewing the research…",
    failed: "The orchestrator could not review the research",
    stillThere: "The report is still on the run’s page.",
  },
  studio_finished: {
    reviewing: "The orchestrator is reviewing the studio’s image…",
    failed: "The orchestrator could not review the studio’s image",
    stillThere: "The image is still on the run’s page.",
  },
  message: {
    reviewing: "The orchestrator is reviewing a run of this chat…",
    failed: "The orchestrator could not review the run",
    stillThere: "What the run made is still on its page.",
  },
};

export function followUpWording(trigger: TurnTrigger): Wording {
  return WORDING[trigger];
}
