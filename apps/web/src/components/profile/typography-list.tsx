import type { Typeface } from "@/lib/api/types";

import { NotFound } from "./profile-value";

type TypographyListProps = { typography: readonly Typeface[] | undefined };

/**
 * The font families the analyzer found, with what each is used for when known. A
 * family is shown as text only, never applied as a style: the font is not loaded
 * here and the value comes from the repository.
 */
export function TypographyList({ typography }: TypographyListProps) {
  if (typography === undefined || typography.length === 0) return <NotFound />;
  return (
    <ul className="grid gap-1.5">
      {typography.map((typeface, index) => (
        <li key={`${typeface.family}:${index}`} className="break-words">
          {typeface.family}
          {typeface.role && <span className="text-muted-foreground"> · {typeface.role}</span>}
        </li>
      ))}
    </ul>
  );
}
