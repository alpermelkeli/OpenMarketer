import { describe, expect, it } from "vitest";

import { shortCommit, versionFromAddress, versionToOpen } from "./version-labels";

const project = { latest_draft_version: null, latest_approved_version: null, unfinished_run_id: null };

describe("the version to open first", () => {
  it("is none for a project without a profile", () => {
    expect(versionToOpen(project)).toBeNull();
  });

  it("is the draft that awaits review", () => {
    expect(versionToOpen({ ...project, latest_draft_version: 3, latest_approved_version: 2 })).toBe(3);
    expect(versionToOpen({ ...project, latest_draft_version: 1 })).toBe(1);
  });

  it("is the approved version when the latest draft is older", () => {
    expect(versionToOpen({ ...project, latest_draft_version: 1, latest_approved_version: 2 })).toBe(2);
    expect(versionToOpen({ ...project, latest_approved_version: 1 })).toBe(1);
  });
});

describe("a version in the address", () => {
  it("is read when it is a positive whole number", () => {
    expect(versionFromAddress("2")).toBe(2);
    expect(versionFromAddress("120")).toBe(120);
  });

  it("is ignored otherwise", () => {
    for (const value of [null, "", "0", "-1", "02", "1.5", "2e3", "two", "1 ", "9999999999"]) {
      expect(versionFromAddress(value)).toBeNull();
    }
  });
});

describe("a commit id", () => {
  it("is shortened to seven characters", () => {
    expect(shortCommit("ecedeaa63b039827bbd703bf604c801395e0b507")).toBe("ecedeaa");
  });
});
