"use client";

import Markdown, { type Components } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

/**
 * The text of a chat message, drawn as the markdown it was written in: emphasis, lists, tables,
 * code and links. Raw HTML is never rendered, an image is shown as a link rather than loaded, and
 * every link opens in a new tab, so no message can reach the network by itself.
 */
export function MessageMarkdown({ text }: { text: string }) {
  return (
    <div className="break-words">
      <Markdown remarkPlugins={[remarkGfm, remarkBreaks]} components={ELEMENTS}>
        {text}
      </Markdown>
    </div>
  );
}

const LINK = "break-all text-brand underline underline-offset-2";

const HEADING = "mt-3 mb-2 font-heading first:mt-0";

const ELEMENTS: Components = {
  p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className={LINK}>
      {children}
    </a>
  ),
  img: ({ src, alt }) => {
    const href = typeof src === "string" ? src : undefined;
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" className={LINK}>
        {alt || href}
      </a>
    );
  },
  code: ({ className, children }) =>
    className?.startsWith("language-") ? (
      <code className={cn("font-mono text-xs", className)}>{children}</code>
    ) : (
      <code className="rounded-sm bg-muted px-1 py-0.5 font-mono text-xs">{children}</code>
    ),
  pre: ({ children }) => (
    <pre className="my-2 overflow-x-auto rounded-md border bg-muted p-3">{children}</pre>
  ),
  ul: ({ className, children }) => (
    <ul className={cn("my-2 pl-5", className?.includes("contains-task-list") ? "list-none" : "list-disc")}>
      {children}
    </ul>
  ),
  ol: ({ children }) => <ol className="my-2 list-decimal pl-5">{children}</ol>,
  input: ({ checked }) => (
    <input type="checkbox" checked={checked === true} disabled className="mr-1.5 size-3.5" />
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-border-strong pl-3 text-muted-foreground">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-4 border-t" />,
  h1: ({ children }) => <h1 className={cn(HEADING, "text-lg")}>{children}</h1>,
  h2: ({ children }) => <h2 className={cn(HEADING, "text-base")}>{children}</h2>,
  h3: ({ children }) => <h3 className={cn(HEADING, "mb-1.5 text-sm")}>{children}</h3>,
  h4: ({ children }) => <h4 className={cn(HEADING, "mb-1.5 text-sm")}>{children}</h4>,
  h5: ({ children }) => <h5 className={cn(HEADING, "mb-1.5 text-sm")}>{children}</h5>,
  h6: ({ children }) => <h6 className={cn(HEADING, "mb-1.5 text-sm")}>{children}</h6>,
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto">
      <table className="w-full border-collapse text-left">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border-b border-border-strong px-2 py-1 font-normal text-muted-foreground">
      {children}
    </th>
  ),
  td: ({ children }) => <td className="border-b border-border px-2 py-1">{children}</td>,
};
