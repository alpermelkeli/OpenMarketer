"use client";

import { useStudioRun } from "@/lib/api/studio";
import type { StudioRun } from "@/lib/api/types";
import type { LibraryTile as Tile } from "@/lib/studio/library";

import { LibraryTile } from "./library-tile";

type FollowedLibraryTileProps = {
  projectId: string;
  /** The run as the list gave it: unfinished, so its state is asked for until it ends. */
  run: StudioRun;
  tile: Tile;
};

/** A tile whose run has not ended: followed on the server until it has, when the library is read again. */
export function FollowedLibraryTile({ projectId, run: listed, tile }: FollowedLibraryTileProps) {
  const { data } = useStudioRun(projectId, listed.id, listed);
  return <LibraryTile projectId={projectId} tile={{ ...tile, run: data ?? listed }} />;
}
