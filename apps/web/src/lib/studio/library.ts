/**
 * The studio's library as a grid: one tile per run, with the asset it made when the library
 * has it, and after them any asset whose run is not among the runs loaded so far. Runs and
 * assets are two lists the API pages on its own, so either can be ahead of the other.
 * Display only; nothing here decides anything about a run or an asset.
 */

import type { Tone } from "@/lib/analysis/run-status";
import type { MediaAsset, StudioRun } from "@/lib/api/types";

import { assetStatusDisplay, isStudioRunFinished, studioRunStatusDisplay } from "./status";

export type LibraryTile = {
  key: string;
  run: StudioRun | null;
  asset: MediaAsset | null;
};

export function libraryTiles(runs: readonly StudioRun[], assets: readonly MediaAsset[]): LibraryTile[] {
  const assetOfRun = new Map(assets.map((asset) => [asset.run_id, asset]));
  const runIds = new Set(runs.map((run) => run.id));
  const tiles: LibraryTile[] = runs.map((run) => ({ key: run.id, run, asset: assetOfRun.get(run.id) ?? null }));
  for (const asset of assets) {
    if (!runIds.has(asset.run_id)) tiles.push({ key: asset.id, run: null, asset });
  }
  return tiles;
}

export type TileState = {
  label: string;
  tone: Tone;
  /** Still running: the dot breathes and the tile is polled. */
  live: boolean;
  /** The run reached its limits and this is its best candidate, waiting for a person. */
  limitsReached: boolean;
};

/** Where a tile stands: the asset's decision once there is an asset, the run's state before. */
export function tileState({ run, asset }: LibraryTile): TileState {
  const limitsReached = run?.status === "needs_review" && asset?.status !== "accepted" && asset?.status !== "rejected";
  if (asset !== null) return { ...assetStatusDisplay(asset.status), live: false, limitsReached };
  if (run === null) return { label: "Unknown", tone: "neutral", live: false, limitsReached: false };
  return { ...studioRunStatusDisplay(run.status), live: !isStudioRunFinished(run), limitsReached: false };
}

/** The version a tile shows: the asset's current one, when there is one. */
export function tileVersionId({ asset }: LibraryTile): string | null {
  return asset?.current_version_id ?? null;
}
