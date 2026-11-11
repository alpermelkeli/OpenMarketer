"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { arrivalHref } from "@/lib/accounts/arrival";
import { callbackProblem, INCOMPLETE_CALLBACK } from "@/lib/accounts/connection-problems";
import { claimOnce } from "@/lib/accounts/once";
import { callbackRequest } from "@/lib/accounts/platforms";
import { OneTimeValue } from "@/lib/api/one-time-value";
import { useFinishConnection } from "@/lib/api/social-accounts";
import type { SocialPlatform } from "@/lib/api/types";
import { isProjectId } from "@/lib/projects/project-id";

import { ConnectionStatus } from "./connection-status";

type ConnectionCallbackScreenProps = { platform: SocialPlatform };

const BACK_TO_PROJECTS = { href: "/", label: "Back to the projects" };

/**
 * Where the platform sends the browser after the person agreed or cancelled. The platform's
 * answer is read from the address once, taken out of it at once, and sent to the API once;
 * then the project's accounts are shown with the outcome. The answer is never shown or logged.
 *
 * It is sent from the browser, through the dashboard's proxy, like every other request: the
 * proxy's origin checks are what tell this page's request from another site's. A ref keeps
 * React's second run of the effect in development from sending it again: the API forgets a
 * connection once it is finished, so a second request would be refused as unknown.
 */
export function ConnectionCallbackScreen({ platform }: ConnectionCallbackScreenProps) {
  const router = useRouter();
  const query = useSearchParams();
  const finish = useFinishConnection(platform);
  const { mutate } = finish;
  const sent = useRef(false);
  // Whether the address held an answer when the page opened; it holds none once the answer is taken out.
  const [answered] = useState(() => callbackRequest(platform, query) !== null);

  useEffect(() => {
    const request = callbackRequest(platform, query);
    if (request === null || !claimOnce(sent)) return;
    // The native call leaves this page mounted; the answer is gone from the address and from history.
    window.history.replaceState(null, "", window.location.pathname);
    mutate(
      {
        state: new OneTimeValue(request.state),
        grant: request.grant === null ? null : new OneTimeValue(request.grant),
      },
      {
        onSuccess: (result) => {
          if (isProjectId(result.project_id)) {
            router.replace(arrivalHref(result.project_id, { outcome: result.outcome, platform }));
          }
        },
      },
    );
  }, [platform, query, mutate, router]);

  if (finish.isError) {
    return <ConnectionStatus state="problem" {...callbackProblem(finish.error)} back={BACK_TO_PROJECTS} />;
  }
  if (finish.isSuccess) {
    return (
      <ConnectionStatus
        state="working"
        title={
          finish.data.outcome === "connected"
            ? "The account is connected. Opening the project's accounts…"
            : "The connection was cancelled. Opening the project's accounts…"
        }
      />
    );
  }
  if (finish.isIdle && !answered) {
    return <ConnectionStatus state="problem" {...INCOMPLETE_CALLBACK} back={BACK_TO_PROJECTS} />;
  }
  return <ConnectionStatus state="working" title="Finishing the connection…" />;
}
