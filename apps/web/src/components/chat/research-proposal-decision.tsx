"use client";

import { CheckIcon, XIcon } from "lucide-react";
import Link from "next/link";

import { Notice } from "@/components/common/notice";
import { Button } from "@/components/ui/button";
import { useDecideProposal } from "@/lib/api/proposals";
import type { ResearchProposal } from "@/lib/chat/events";
import { proposalText } from "@/lib/chat/research-progress";
import { projectPageHref } from "@/lib/projects/project-navigation";
import { decideProblem, effectLabel } from "@/lib/proposals/proposal-display";

type ResearchProposalDecisionProps = { projectId: string; proposal: ResearchProposal };

/**
 * The action a research run asked to run that the policy gate held for a person, right in the run's
 * card: what it would do, the buttons to decide it, and the way to the project's approvals screen
 * for what exactly it asks. The decision is the person's own request to the API; the card then
 * shows where it stands, as the chat's events say. Nothing here decides anything itself.
 */
export function ResearchProposalDecision({ projectId, proposal }: ResearchProposalDecisionProps) {
  const decide = useDecideProposal(projectId);
  const waiting = proposal.status === "waiting";

  return (
    <div className="grid gap-2 rounded-sm border border-dashed border-warning/60 bg-surface p-3">
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-2xs tracking-widest text-muted-foreground uppercase">
          Approval
        </span>
        <span className="font-mono text-xs">{proposal.actionName}</span>
        {(proposal.effect === "publishes" || proposal.effect === "spends") && (
          <span className="text-2xs tracking-widest text-muted-foreground uppercase">
            {effectLabel(proposal.effect)}
          </span>
        )}
      </p>
      <p className="text-pretty">{proposalText(proposal)}</p>
      {waiting && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <Button
            variant="outline"
            size="sm"
            disabled={decide.isPending}
            onClick={() => decide.mutate({ proposalId: proposal.id, approved: false })}
          >
            <XIcon data-icon="inline-start" aria-hidden="true" />
            Reject
          </Button>
          <Button
            size="sm"
            disabled={decide.isPending}
            onClick={() => decide.mutate({ proposalId: proposal.id, approved: true })}
          >
            <CheckIcon data-icon="inline-start" aria-hidden="true" />
            Approve
          </Button>
          <Link
            href={projectPageHref(projectId, "approvals")}
            className="rounded-sm text-xs text-muted-foreground underline-offset-4 hover:underline"
          >
            What exactly it asks
          </Link>
        </div>
      )}
      {decide.error !== null && waiting && (
        <Notice tone="danger" role="status">
          <p className="text-pretty">{decideProblem(decide.error)}</p>
        </Notice>
      )}
    </div>
  );
}
