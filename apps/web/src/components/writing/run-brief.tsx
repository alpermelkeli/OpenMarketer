import { Skeleton } from "@/components/ui/skeleton";
import { type RunBrief as Brief, variantsText } from "@/lib/writing/brief";
import { goalLabel, writingPlatformLabel } from "@/lib/writing/status";

/**
 * What the run was asked for: its goal, the feature it announces or the person's angle, the
 * platform and how many texts. The angle is what a person wrote, shown as text.
 */
export function RunBrief({ brief }: { brief: Brief }) {
  return (
    <section aria-labelledby="brief" className="grid max-w-prose gap-4 border-t pt-8">
      <h2 id="brief" className="font-heading text-lg">
        What was asked
      </h2>
      {brief.state === "loading" && <Skeleton className="h-24" />}
      {brief.state === "unreadable" && (
        <p className="text-sm text-pretty text-muted-foreground">
          The brief of this run is stored in a form this version of the API cannot read, so it cannot be shown. The
          plan below is what the writer made of it.
        </p>
      )}
      {brief.state === "known" && <BriefFields brief={brief} />}
    </section>
  );
}

function BriefFields({ brief: { brief } }: { brief: Extract<Brief, { state: "known" }> }) {
  return (
    <dl className="grid gap-4 text-sm sm:grid-cols-[9rem_minmax(0,1fr)] sm:gap-x-6">
      <Term>Goal</Term>
      <dd>{goalLabel(brief.goal)}</dd>

      {brief.goal === "announce" && (
        <>
          <Term>Feature</Term>
          <dd className="font-mono break-all">{brief.feature ?? "None named"}</dd>
        </>
      )}

      {brief.goal === "angle" && (
        <>
          <Term>Your angle</Term>
          <dd className="break-words whitespace-pre-wrap">{brief.angle ?? ""}</dd>
        </>
      )}

      <Term>Platform</Term>
      <dd>{writingPlatformLabel(brief.platform)}</dd>

      <Term>Texts</Term>
      <dd>{variantsText(brief.variants)}</dd>
    </dl>
  );
}

function Term({ children }: { children: string }) {
  return <dt className="text-2xs tracking-widest text-muted-foreground uppercase sm:pt-0.5">{children}</dt>;
}
