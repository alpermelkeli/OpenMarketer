import { postLengthOf, variantLabel, type Alternative } from "@/lib/writing/record";

import { CodeChecks, CriticReview, FlaggedNumbers } from "./post-checks";

/**
 * The other texts of the round the post was chosen from, each with what the code and the critic
 * found of it. A person reads them to see what was weighed; the gate approves or rejects the post
 * only, so none of them can be chosen here. Every text was written by a model: shown as text only.
 */
export function PostAlternatives({ alternatives }: { alternatives: readonly Alternative[] }) {
  return (
    <section aria-labelledby="alternatives" className="grid gap-4 border-t pt-8">
      <div className="max-w-prose">
        <h2 id="alternatives" className="font-heading text-lg">
          The other texts
        </h2>
        <p className="mt-1 text-sm text-pretty text-muted-foreground">
          What else the writer wrote in the round the post was chosen from. They are here to read: a post is approved
          or rejected as it is, and another text cannot be picked instead.
        </p>
      </div>
      {alternatives.length === 0 ? (
        <p className="max-w-prose text-sm text-muted-foreground">There were no other texts in the round.</p>
      ) : (
        <ol className="grid gap-6">
          {alternatives.map((alternative) => {
            const length = postLengthOf(alternative.check);
            return (
              <li key={alternative.index} className="grid max-w-prose gap-3 border-l pl-4">
                <p className="text-2xs tracking-widest text-muted-foreground uppercase">
                  {variantLabel(alternative.index)}
                </p>
                <p className="text-pretty break-words whitespace-pre-wrap">{alternative.text}</p>
                <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                  {length !== null && (
                    <span className={length.kept === false ? "text-destructive" : undefined}>
                      Counts <span className="font-mono tabular-nums">{length.count}</span> as X counts it
                      {length.kept === false && ", over its limit"}
                    </span>
                  )}
                  {alternative.language !== "" && (
                    <span>
                      Language <span className="font-mono">{alternative.language}</span>
                    </span>
                  )}
                  {alternative.angle !== "" && <span className="text-pretty">Angle: {alternative.angle}</span>}
                </p>
                {alternative.claims.length > 0 && (
                  <ul aria-label="Its claims" className="grid gap-0.5 text-xs">
                    {alternative.claims.map((claim, index) => (
                      <li key={index} className="text-pretty">
                        {claim.statement}
                        {claim.featureIds.length > 0 && (
                          <span className="text-muted-foreground">
                            {" "}
                            · <span className="font-mono">{claim.featureIds.join(", ")}</span>
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                <FlaggedNumbers numbers={alternative.check?.flaggedNumbers ?? []} />
                <CodeChecks report={alternative.check} />
                <CriticReview
                  review={alternative.review}
                  missing={
                    alternative.check?.passed === false
                      ? "Not reviewed: a text that breaks a rule of the code is not shown to the critic."
                      : "No review by the critic was stored for this text."
                  }
                />
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
