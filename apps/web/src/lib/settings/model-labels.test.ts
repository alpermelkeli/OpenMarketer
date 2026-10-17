import { describe, expect, it } from "vitest";

import { problemText, roleLabel, roleSourceDisplay, tierSourceDisplay } from "./model-labels";

describe("the words for a role", () => {
  it("say which role runs today", () => {
    expect(roleLabel("repo_analyzer")).toMatchObject({ label: "Repository analyzer", inUse: true });
    expect(roleLabel("researcher").inUse).toBe(true);
    expect(roleLabel("reader").inUse).toBe(true);
    expect(roleLabel("writer").inUse).toBe(false);
  });

  it("say that the screener runs today and takes a model only", () => {
    expect(roleLabel("screener")).toMatchObject({ label: "Screener", inUse: true });
    expect(roleLabel("screener").purpose).toContain("takes a model only");
  });

  it("say that the orchestrator and the studio's roles run today", () => {
    for (const role of ["orchestrator", "creative_director", "studio", "asset_verifier", "image_generator"]) {
      expect(roleLabel(role).inUse).toBe(true);
    }
    expect(roleLabel("image_generator").purpose).toContain("takes a model only");
  });

  it("call a role the dashboard does not know by its name", () => {
    expect(roleLabel("poet")).toMatchObject({ label: "poet", inUse: false });
    expect(roleLabel("constructor").label).toBe("constructor");
  });
});

describe("where a model comes from", () => {
  it("is said in words, with the environment as a warning", () => {
    expect(tierSourceDisplay("workspace")).toEqual({ label: "Chosen here", tone: "success" });
    expect(tierSourceDisplay("environment").tone).toBe("warning");
  });

  it("names the tier a role follows", () => {
    expect(roleSourceDisplay("tier", "fast").label).toBe("Follows the fast tier");
    expect(roleSourceDisplay("file", null).label).toBe("From the configuration file");
  });
});

describe("a problem the check found", () => {
  it("says what to do about a temperature the model rejects", () => {
    expect(problemText("rejects_temperature")).toContain("rejecting the temperature");
  });

  it("does not tell a fallback's to mark it, which would not help", () => {
    expect(problemText("fallback_rejects_temperature")).not.toContain("Mark it");
  });
});
