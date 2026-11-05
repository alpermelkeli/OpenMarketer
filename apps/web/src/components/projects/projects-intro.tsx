type ProjectsIntroProps = {
  /** Whether the workspace has no project yet: the words say hello, and the form is already there. */
  empty: boolean;
};

const STEPS = [
  ["Point it at a repository", "Give the project a name and the https address of its code."],
  ["Let it read", "An analysis drafts a Product Profile, with the file and lines behind every claim."],
  ["You decide", "Check what it is unsure about, correct it, and approve. Nothing counts until you do."],
] as const;

/** The landing of the root screen: what OpenMarketer does, and the three steps that start it. */
export function ProjectsIntro({ empty }: ProjectsIntroProps) {
  return (
    <div>
      <h1 className="font-heading text-display text-balance">
        {empty ? "Nothing here yet. Let's meet your product." : "Marketing that starts from your code."}
      </h1>
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
  );
}
