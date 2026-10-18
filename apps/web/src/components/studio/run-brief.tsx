import { Skeleton } from "@/components/ui/skeleton";
import { type RunBrief as Brief, copyLines, referencesText, styleNotesText } from "@/lib/studio/brief";
import { platformLabel } from "@/lib/studio/status";

/**
 * What the run was asked for: its goal, the words the image must show, the platform and the notes
 * on style. Everything here is what a person or a model wrote, shown as text.
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
          The request of this run is stored in a form this version of the API cannot read, so it cannot be shown. The
          prompts of the versions below are what the studio wrote from it.
        </p>
      )}
      {brief.state === "known" && <BriefFields brief={brief} />}
    </section>
  );
}

function BriefFields({ brief: { request } }: { brief: Extract<Brief, { state: "known" }> }) {
  const lines = copyLines(request);
  return (
    <dl className="grid gap-4 text-sm sm:grid-cols-[9rem_minmax(0,1fr)] sm:gap-x-6">
      <Term>Goal</Term>
      <dd className="break-words whitespace-pre-wrap">{request.goal}</dd>

      <Term>Words on the image</Term>
      <dd>
        {lines.length === 0 ? (
          <span className="text-muted-foreground">No words were asked for.</span>
        ) : (
          <ul className="grid gap-1">
            {lines.map((line, index) => (
              <li key={index}>
                <q className="break-words">{line}</q>
              </li>
            ))}
          </ul>
        )}
      </dd>

      <Term>Platform</Term>
      <dd>{platformLabel(request.platform)}</dd>

      <Term>Notes on style</Term>
      <dd
        className={
          request.style_notes.trim() === "" ? "text-muted-foreground" : "break-words whitespace-pre-wrap"
        }
      >
        {styleNotesText(request)}
      </dd>

      <Term>To look at</Term>
      <dd className="text-muted-foreground">{referencesText(request)}</dd>
    </dl>
  );
}

function Term({ children }: { children: string }) {
  return <dt className="text-2xs tracking-widest text-muted-foreground uppercase sm:pt-0.5">{children}</dt>;
}
