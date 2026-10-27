import { EvidenceList } from "@/components/profile/evidence-list";
import type { EvidenceSource } from "@/lib/profile/evidence-link";
import type { Claim } from "@/lib/writing/record";

type PostClaimsProps = {
  claims: readonly Claim[];
  /** Where the evidence is linked to; a repository on no known host gives paths without links. */
  source: EvidenceSource;
};

/**
 * What the post claims about the product, each with the live features it rests on and where in the
 * repository each was found. The statements were written by a model and the paths come from the
 * profile: shown as text, linked only through `evidenceLink`.
 */
export function PostClaims({ claims, source }: PostClaimsProps) {
  return (
    <section aria-labelledby="claims" className="grid gap-4 border-t pt-8">
      <div className="max-w-prose">
        <h2 id="claims" className="font-heading text-lg">
          What it claims
        </h2>
        <p className="mt-1 text-sm text-pretty text-muted-foreground">
          Every claim must rest on a feature the approved profile holds as live; code checked that it names one. Read
          the evidence for any claim you are unsure of.
        </p>
      </div>
      {claims.length === 0 ? (
        <p className="max-w-prose text-sm text-muted-foreground">The writer said the post makes no claim about the product.</p>
      ) : (
        <ul className="grid max-w-prose gap-4">
          {claims.map((claim, index) => (
            <li key={index} className="grid gap-1.5">
              <p className="text-pretty whitespace-pre-wrap">{claim.statement}</p>
              <p className="text-xs text-muted-foreground">
                {claim.featureIds.length === 0 ? (
                  <span className="text-warning">Names no feature.</span>
                ) : (
                  <>
                    Rests on{" "}
                    {claim.featureIds.map((id, at) => (
                      <span key={id}>
                        {at > 0 && ", "}
                        <span className="font-mono text-foreground">{id}</span>
                      </span>
                    ))}
                  </>
                )}
              </p>
              <EvidenceList evidence={claim.evidence} source={source} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
