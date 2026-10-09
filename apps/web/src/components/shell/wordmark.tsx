import Link from "next/link";

/** OpenMarketer's name with its mark: a ring and the signal it sends out. */
export function Wordmark() {
  return (
    <Link href="/" className="inline-flex items-center gap-2.5 rounded-sm" aria-label="OpenMarketer, all projects">
      <svg viewBox="0 0 24 24" className="size-5 text-brand" aria-hidden="true">
        <circle cx="10" cy="14" r="6.25" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="19" cy="5" r="2.5" fill="currentColor" />
      </svg>
      <span className="font-heading text-lg leading-none tracking-tight">OpenMarketer</span>
    </Link>
  );
}
