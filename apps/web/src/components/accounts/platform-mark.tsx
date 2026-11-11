import type { ComponentType } from "react";

import type { SocialPlatform } from "@/lib/api/types";
import { cn } from "@/lib/utils";

type MarkProps = { className?: string };

/** X's mark, drawn in the text's colour. */
function XMark({ className }: MarkProps) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={className} fill="currentColor">
      <path d="M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z" />
    </svg>
  );
}

// A record over the API's platforms, so a platform it adds fails the type check until it has a mark.
const MARKS: Record<SocialPlatform, ComponentType<MarkProps>> = { x: XMark };

/** The small mark of a platform beside its name. Decorative: the name is always written next to it. */
export function PlatformMark({ platform, className }: { platform: SocialPlatform; className?: string }) {
  const Mark = MARKS[platform];
  return <Mark className={cn("size-4 shrink-0", className)} />;
}
