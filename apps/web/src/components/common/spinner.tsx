import { LoaderCircleIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/** Turning while something runs; the page's reduced-motion rule stills it. */
export function Spinner({ className }: { className?: string }) {
  return <LoaderCircleIcon aria-hidden="true" className={cn("size-3.5 shrink-0 animate-spin text-brand", className)} />;
}
