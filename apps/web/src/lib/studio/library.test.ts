import { describe, expect, it } from "vitest";

import type { MediaAsset, StudioRun } from "@/lib/api/types";

import { libraryTiles, tileState, tileVersionId } from "./library";

function run(id: string, status: StudioRun["status"] = "succeeded"): StudioRun {
  return {
    id,
    project_id: "p",
    status,
    platform: "instagram_post",
    requested_by: "person",
    error: null,
    final_asset_id: null,
    cost_usd: 0.08,
    created_at: "2026-10-18T12:00:00Z",
    started_at: null,
    finished_at: null,
  };
}

function asset(id: string, runId: string, status: MediaAsset["status"] = "draft"): MediaAsset {
  return {
    id,
    project_id: "p",
    run_id: runId,
    platform: "instagram_post",
    status,
    current_version_id: `${id}-v`,
    decided_by: null,
    decided_at: null,
    created_at: "2026-10-18T12:05:00Z",
  };
}

describe("the tiles of the library", () => {
  it("are the runs in their order, each with the asset it made", () => {
    const tiles = libraryTiles([run("r2"), run("r1", "running")], [asset("a2", "r2")]);
    expect(tiles.map((tile) => [tile.run?.id, tile.asset?.id ?? null])).toEqual([
      ["r2", "a2"],
      ["r1", null],
    ]);
  });

  it("keep an asset whose run is not loaded yet, after the runs", () => {
    const tiles = libraryTiles([run("r2")], [asset("a2", "r2"), asset("a0", "r0")]);
    expect(tiles.map((tile) => tile.key)).toEqual(["r2", "a0"]);
    expect(tiles[1].run).toBeNull();
  });

  it("show the asset's current version", () => {
    expect(tileVersionId({ key: "r", run: run("r"), asset: asset("a", "r") })).toBe("a-v");
    expect(tileVersionId({ key: "r", run: run("r"), asset: null })).toBeNull();
  });
});

describe("where a tile stands", () => {
  it("is the run's state, live, until there is an asset", () => {
    expect(tileState({ key: "r", run: run("r", "running"), asset: null })).toMatchObject({
      label: "Making the image",
      live: true,
    });
    expect(tileState({ key: "r", run: run("r", "failed"), asset: null })).toMatchObject({ tone: "danger", live: false });
  });

  it("is the asset's decision once there is one", () => {
    expect(tileState({ key: "r", run: run("r"), asset: asset("a", "r", "accepted") }).label).toBe("Accepted");
  });

  it("says the limits were reached while a person has still to decide", () => {
    expect(tileState({ key: "r", run: run("r", "needs_review"), asset: asset("a", "r") })).toMatchObject({
      label: "Draft",
      limitsReached: true,
    });
    expect(tileState({ key: "r", run: run("r", "needs_review"), asset: asset("a", "r", "accepted") }).limitsReached).toBe(
      false,
    );
  });
});
