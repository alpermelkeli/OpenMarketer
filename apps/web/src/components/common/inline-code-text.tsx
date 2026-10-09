import { Fragment } from "react";

type InlineCodeTextProps = {
  /** The dashboard's own wording, with commands between backticks. Never text from a repository. */
  text: string;
};

/** A sentence of the dashboard's own in which `make worker` is set as code. */
export function InlineCodeText({ text }: InlineCodeTextProps) {
  return (
    <>
      {text.split("`").map((part, index) =>
        index % 2 === 1 ? <code key={index}>{part}</code> : <Fragment key={index}>{part}</Fragment>,
      )}
    </>
  );
}
