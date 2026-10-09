import type { ReactNode } from "react";

type ProfileValueProps = {
  label: string;
  children: ReactNode;
};

/** One labelled value of a section, as a row of its definition list. */
export function ProfileValue({ label, children }: ProfileValueProps) {
  return (
    <div className="contents">
      <dt className="text-sm text-muted-foreground sm:pt-0.5">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  );
}

/** Shown where the analyzer found nothing. Saying so is part of the review. */
export function NotFound() {
  return <span className="text-muted-foreground">Not found</span>;
}

type TextValueProps = { text: string | null | undefined };

/** A text from the profile, as text. */
export function TextValue({ text }: TextValueProps) {
  if (text === null || text === undefined || text === "") return <NotFound />;
  return <p className="max-w-prose text-pretty break-words whitespace-pre-wrap">{text}</p>;
}

type TagListProps = { values: readonly string[] | undefined };

/** Short values from the profile (platforms, languages), each as a small tag of text. */
export function TagList({ values }: TagListProps) {
  if (values === undefined || values.length === 0) return <NotFound />;
  return (
    <ul className="flex flex-wrap gap-1.5">
      {values.map((value, index) => (
        <li key={`${value}:${index}`} className="rounded-sm border px-2 py-0.5 font-mono text-xs break-all">
          {value}
        </li>
      ))}
    </ul>
  );
}

/** Longer values from the profile (pains), one per line. */
export function TextList({ values }: TagListProps) {
  if (values === undefined || values.length === 0) return <NotFound />;
  return (
    <ul className="grid max-w-prose list-disc gap-1.5 pl-4 marker:text-border-strong">
      {values.map((value, index) => (
        <li key={`${value}:${index}`} className="text-pretty break-words">
          {value}
        </li>
      ))}
    </ul>
  );
}
