import type { Metadata } from "next";
import { Suspense } from "react";

import { ConnectionCallbackScreen } from "@/components/accounts/connection-callback-screen";
import { ConnectionStatus } from "@/components/accounts/connection-status";
import { UNKNOWN_PLATFORM } from "@/lib/accounts/connection-problems";
import { isSocialPlatform } from "@/lib/accounts/platforms";

export const metadata: Metadata = { title: "Connecting an account" };

type Props = PageProps<"/connections/[platform]/callback">;

const WORKING = <ConnectionStatus state="working" title="Finishing the connection…" />;

/** Where a platform sends the browser back after a person agreed to connect an account, or cancelled. */
export default function ConnectionCallbackPage({ params }: Props) {
  return (
    <Suspense fallback={WORKING}>
      <CallbackOfPlatform params={params} />
    </Suspense>
  );
}

// The platform is known only at request time, and the screen reads the platform's answer from
// the address, so both are inside the boundary.
async function CallbackOfPlatform({ params }: Pick<Props, "params">) {
  const { platform } = await params;
  if (!isSocialPlatform(platform)) {
    return <ConnectionStatus state="problem" {...UNKNOWN_PLATFORM} back={{ href: "/", label: "Back to the projects" }} />;
  }
  return <ConnectionCallbackScreen platform={platform} />;
}
