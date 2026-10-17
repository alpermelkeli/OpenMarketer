import { isStudioRunFinished } from "@/lib/studio/status";
import type { LibraryTile as Tile } from "@/lib/studio/library";

import { FollowedLibraryTile } from "./followed-library-tile";
import { LibraryTile } from "./library-tile";

type LibraryGridProps = { projectId: string; tiles: readonly Tile[] };

/** The library's images in a quiet grid, newest first. */
export function LibraryGrid({ projectId, tiles }: LibraryGridProps) {
  return (
    <ul aria-label="Images" className="grid grid-cols-1 gap-x-6 gap-y-8 sm:grid-cols-2 lg:grid-cols-3">
      {tiles.map((tile) =>
        tile.run !== null && !isStudioRunFinished(tile.run) ? (
          <FollowedLibraryTile key={tile.key} projectId={projectId} run={tile.run} tile={tile} />
        ) : (
          <LibraryTile key={tile.key} projectId={projectId} tile={tile} />
        ),
      )}
    </ul>
  );
}
