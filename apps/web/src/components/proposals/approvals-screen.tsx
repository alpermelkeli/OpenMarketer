"use client";

import { useState } from "react";

import { LoadMore } from "@/components/common/load-more";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import { useDecideProposal, useProposals } from "@/lib/api/proposals";
import type { Proposal } from "@/lib/api/types";
import { decideProblem, splitByDecision } from "@/lib/proposals/proposal-display";
import { useNow } from "@/lib/use-now";

import { DecideDialog } from "./decide-dialog";
import { ProposalCard } from "./proposal-card";

type ApprovalsScreenProps = { projectId: string };

type Asked = { proposal: Proposal; approved: boolean };

/** The actions the project's agents asked for that wait for a person, and those already decided. */
export function ApprovalsScreen({ projectId }: ApprovalsScreenProps) {
  const proposals = useProposals(projectId);
  const decide = useDecideProposal(projectId);
  const [asked, setAsked] = useState<Asked | null>(null);
  const now = useNow(true);

  const split = proposals.data === undefined ? undefined : splitByDecision(proposals.data);

  function open(proposal: Proposal, approved: boolean) {
    decide.reset();
    setAsked({ proposal, approved });
  }

  function confirm() {
    if (asked === null) return;
    decide.mutate(
      { proposalId: asked.proposal.id, approved: asked.approved },
      { onSuccess: () => setAsked(null) },
    );
  }

  return (
    <div className="grid gap-8">
      <section aria-labelledby="waiting" className="grid gap-4">
        <div className="max-w-prose">
          <h2 id="waiting" className="font-heading text-title">
            Waiting for your decision
          </h2>
          <p className="mt-2 text-pretty text-muted-foreground">
            An agent never publishes or spends on its own. It asks here first, and its run waits until you approve or
            reject, or until the deadline passes, which counts as a rejection. Read exactly what it asks before you
            decide.
          </p>
        </div>
        {proposals.error !== null && (
          <RequestProblem
            title="The proposals could not be read"
            error={proposals.error}
            onRetry={() => void proposals.refetch()}
          />
        )}
        {split === undefined && proposals.error === null && <Skeleton className="h-40" />}
        {split?.waiting.length === 0 && (
          <p className="max-w-prose text-sm text-pretty text-muted-foreground">
            Nothing is waiting. No agent of this project has asked to publish or spend.
          </p>
        )}
        {split?.waiting.map((proposal) => (
          <ProposalCard
            key={proposal.id}
            proposal={proposal}
            now={now}
            onDecide={(approved) => open(proposal, approved)}
          />
        ))}
      </section>

      {split !== undefined && split.decided.length > 0 && (
        <section aria-labelledby="decided" className="grid gap-4 border-t pt-8">
          <h2 id="decided" className="font-heading text-lg">
            Decided
          </h2>
          {split.decided.map((proposal) => (
            <ProposalCard key={proposal.id} proposal={proposal} now={now} />
          ))}
        </section>
      )}

      <LoadMore
        noun="proposals"
        hasMore={proposals.hasNextPage}
        loading={proposals.isFetchingNextPage}
        onLoadMore={() => void proposals.fetchNextPage()}
      />

      {asked !== null && (
        <DecideDialog
          proposal={asked.proposal}
          approving={asked.approved}
          onOpenChange={(isOpen) => !isOpen && !decide.isPending && setAsked(null)}
          pending={decide.isPending}
          problem={decide.error === null ? null : decideProblem(decide.error)}
          onConfirm={confirm}
        />
      )}
    </div>
  );
}
