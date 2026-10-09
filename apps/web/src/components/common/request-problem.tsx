import { Notice } from "@/components/common/notice";
import { Button } from "@/components/ui/button";
import type { ApiError } from "@/lib/api/api-error";
import { errorMessage } from "@/lib/api/error-message";

import { InlineCodeText } from "./inline-code-text";

type RequestProblemProps = {
  /** What could not be read, as a sentence: "The projects could not be read". */
  title: string;
  error: ApiError;
  onRetry: () => void;
};

/** A read from the API that failed, with what to do about it and a way to try again. */
export function RequestProblem({ title, error, onRetry }: RequestProblemProps) {
  return (
    <Notice tone="danger" title={title} role="alert">
      <p>
        <InlineCodeText text={errorMessage(error)} />
      </p>
      <Button variant="outline" size="sm" className="mt-3" onClick={onRetry}>
        Try again
      </Button>
    </Notice>
  );
}
