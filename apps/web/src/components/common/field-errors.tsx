type FieldErrorsProps = {
  /** Referenced by the field's `aria-describedby`. */
  id: string;
  messages: readonly string[];
};

/** The messages about one field, announced when they appear. */
export function FieldErrors({ id, messages }: FieldErrorsProps) {
  return (
    <div id={id} role="alert" className="text-sm text-destructive empty:hidden">
      {messages.map((message) => (
        <p key={message} className="text-pretty">
          {message}
        </p>
      ))}
    </div>
  );
}
