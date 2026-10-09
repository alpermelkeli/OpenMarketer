import { Button } from "@/components/ui/button";

type LoadMoreProps = {
  /** What the list holds, in the plural: "projects", "runs", "versions". */
  noun: string;
  hasMore: boolean;
  loading: boolean;
  onLoadMore: () => void;
};

/** The end of a list that is loaded a page at a time. Renders nothing when the list is complete. */
export function LoadMore({ noun, hasMore, loading, onLoadMore }: LoadMoreProps) {
  if (!hasMore) return null;
  return (
    <div className="pt-2">
      <Button variant="outline" onClick={onLoadMore} disabled={loading}>
        {loading ? "Loading…" : `Load more ${noun}`}
      </Button>
    </div>
  );
}
