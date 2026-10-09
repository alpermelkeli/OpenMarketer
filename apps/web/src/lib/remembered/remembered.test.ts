import { describe, expect, it } from "vitest";

import type { Project } from "@/lib/api/types";

import {
  NOTHING_REMEMBERED,
  findProject,
  parseRemembered,
  withOpenedProject,
  withProject,
  withRun,
  withoutProject,
} from "./remembered";

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const RUN = "33333333-3333-4333-8333-333333333333";

function project(id: string, name = "Memoria"): Project {
  return { id, name, repository_url: "https://github.com/o/r", created_at: "2026-10-08T12:00:00Z" };
}

describe("reading what was stored", () => {
  it("is empty when nothing or nonsense was stored", () => {
    for (const stored of [null, "", "{", "[]", '"text"', "null"]) {
      expect(parseRemembered(stored)).toEqual(NOTHING_REMEMBERED);
    }
  });

  it("returns what was written", () => {
    const remembered = withRun(withProject(NOTHING_REMEMBERED, project(A)), A, RUN);
    expect(parseRemembered(JSON.stringify(remembered))).toEqual(remembered);
  });

  it("drops entries that are not what the dashboard writes", () => {
    const stored = JSON.stringify({
      projects: [project(A), { id: "x", name: "bad id" }, { ...project(B), name: 5 }, null],
      openedProjectIds: [B, "nope", 7],
      runIdsByProject: { [A]: [RUN, "nope"], "not-a-project": [RUN], [B]: "nope" },
    });
    expect(parseRemembered(stored)).toEqual({
      projects: [project(A)],
      openedProjectIds: [B],
      runIdsByProject: { [A]: [RUN], [B]: [] },
    });
  });
});

describe("remembering", () => {
  it("puts the newest project first and keeps one entry per project", () => {
    const first = withProject(withProject(NOTHING_REMEMBERED, project(A)), project(B));
    const renamed = withProject(first, project(A, "Renamed"));
    expect(renamed.projects.map((p) => p.id)).toEqual([A, B]);
    expect(findProject(renamed, A)?.name).toBe("Renamed");
  });

  it("keeps a project opened by id once, and not beside the full project", () => {
    const opened = withOpenedProject(withOpenedProject(NOTHING_REMEMBERED, A), A);
    expect(opened.openedProjectIds).toEqual([A]);
    expect(withProject(opened, project(A)).openedProjectIds).toEqual([]);
    expect(withOpenedProject(withProject(NOTHING_REMEMBERED, project(A)), A).openedProjectIds).toEqual([]);
  });

  it("puts the newest run first and keeps each run once", () => {
    const other = "44444444-4444-4444-8444-444444444444";
    const runs = withRun(withRun(withRun(NOTHING_REMEMBERED, A, RUN), A, other), A, RUN);
    expect(runs.runIdsByProject[A]).toEqual([other, RUN]);
  });

  it("forgets a project together with its runs, and nothing else", () => {
    const both = withRun(withProject(withOpenedProject(NOTHING_REMEMBERED, B), project(A)), A, RUN);
    expect(withoutProject(both, A)).toEqual({ projects: [], openedProjectIds: [B], runIdsByProject: {} });
  });
});
