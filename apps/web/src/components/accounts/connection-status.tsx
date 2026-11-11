import Link from "next/link";

import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { Spinner } from "@/components/common/spinner";

type ConnectionStatusProps =
  | { state: "working"; title: string }
  | { state: "problem"; title: string; message: string; back: { href: string; label: string } };

/** The callback page's one message, set in the middle of the column: what is happening, or why it stopped. */
export function ConnectionStatus(props: ConnectionStatusProps) {
  return (
    <div className="grid min-h-[50dvh] place-items-center">
      <div className="grid w-full max-w-prose gap-4">
        <h1 className="font-heading text-display text-balance">Connecting an account</h1>
        {props.state === "working" ? (
          <p role="status" className="flex items-center gap-2 text-muted-foreground">
            <Spinner />
            {props.title}
          </p>
        ) : (
          <Notice tone="danger" title={props.title} role="alert">
            <p>
              <InlineCodeText text={props.message} />
            </p>
            <p className="mt-3">
              <Link href={props.back.href} className="rounded-sm text-brand underline-offset-4 hover:underline">
                {props.back.label}
              </Link>
            </p>
          </Notice>
        )}
      </div>
    </div>
  );
}
