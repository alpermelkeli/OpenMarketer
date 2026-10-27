import type { Plan } from "@/lib/writing/record";

type PostPlanProps = {
  plan: Plan;
  /** What the code changed in the writer's plan to hold it to the brief and the project. */
  changes: readonly string[];
};

/**
 * The plan the texts were written from, and what the code changed in it. The angle was written by
 * a model and each change is the code's sentence: both shown as text.
 */
export function PostPlan({ plan, changes }: PostPlanProps) {
  return (
    <section aria-labelledby="plan" className="grid max-w-prose gap-4 border-t pt-8">
      <h2 id="plan" className="font-heading text-lg">
        The plan
      </h2>
      <dl className="grid gap-4 text-sm sm:grid-cols-[9rem_minmax(0,1fr)] sm:gap-x-6">
        <Term>Angle</Term>
        <dd className={plan.angle === null ? "text-muted-foreground" : "text-pretty whitespace-pre-wrap"}>
          {plan.angle ?? "Not recorded."}
        </dd>

        <Term>Format</Term>
        <dd className={plan.format === null ? "text-muted-foreground" : undefined}>{plan.format ?? "Not recorded."}</dd>

        <Term>Features</Term>
        <dd>
          {plan.featureIds.length === 0 ? (
            <span className="text-muted-foreground">None: the plan makes no claim about a feature.</span>
          ) : (
            <ul className="grid gap-0.5 font-mono">
              {plan.featureIds.map((id) => (
                <li key={id} className="break-all">
                  {id}
                </li>
              ))}
            </ul>
          )}
        </dd>

        <Term>Changed by code</Term>
        <dd>
          {changes.length === 0 ? (
            <span className="text-muted-foreground">Nothing: the plan kept to the brief as the writer made it.</span>
          ) : (
            <ul className="grid list-disc gap-0.5 pl-5">
              {changes.map((change, index) => (
                <li key={index} className="text-pretty">
                  {change}
                </li>
              ))}
            </ul>
          )}
        </dd>
      </dl>
    </section>
  );
}

function Term({ children }: { children: string }) {
  return <dt className="text-2xs tracking-widest text-muted-foreground uppercase sm:pt-0.5">{children}</dt>;
}
