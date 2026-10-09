import type { ReactNode } from "react";

type ProjectsEmptyStateProps = {
  /** The form that creates the first project. */
  children: ReactNode;
};

const STEPS = [
  ["Point it at a repository", "Give the project a name and the https address of its code."],
  ["Let it read", "An analysis drafts a Product Profile, with the file and lines behind every claim."],
  ["You decide", "Check what it is unsure about, correct it, and approve. Nothing counts until you do."],
] as const;

/** The first thing a new user sees: what OpenMarketer will do, and the one form that starts it. */
export function ProjectsEmptyState({ children }: ProjectsEmptyStateProps) {
  return (
    <div className="grid animate-enter gap-12 border-t pt-12 md:grid-cols-[1fr_minmax(0,22rem)] md:gap-16">
      <div>
        <h2 className="font-heading text-title">Nothing here yet. Let&rsquo;s meet your product.</h2>
        <p className="mt-2 max-w-prose text-pretty text-muted-foreground">
          OpenMarketer starts by reading your code, so that everything it later says about your product can be
          traced back to a line you wrote.
        </p>
        <ol className="mt-8 grid gap-6">
          {STEPS.map(([title, text], index) => (
            <li key={title} className="flex gap-4">
              <span
                aria-hidden="true"
                className="flex size-6 shrink-0 items-center justify-center rounded-full border border-border-strong font-mono text-2xs text-muted-foreground"
              >
                {index + 1}
              </span>
              <div>
                <p>{title}</p>
                <p className="text-sm text-pretty text-muted-foreground">{text}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>
      <section aria-labelledby="first-project" className="rounded-md border bg-card p-6">
        <h2 id="first-project" className="mb-5 font-heading text-lg">
          Your first project
        </h2>
        {children}
      </section>
    </div>
  );
}
