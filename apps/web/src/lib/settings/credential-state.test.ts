import { describe, expect, it } from "vitest";

import { credentialOffer, credentialSourceDisplay } from "./credential-state";

const STORED = { set_at: "2026-10-08T12:00:00Z", set_by: "00000000-0000-4000-8000-000000000001" };

describe("where a credential's value comes from", () => {
  it("is said in words, and in colour only when a value is in use", () => {
    expect(credentialSourceDisplay("not_set")).toEqual({ label: "Not set", tone: "neutral" });
    expect(credentialSourceDisplay("stored")).toEqual({ label: "Set here", tone: "success" });
    expect(credentialSourceDisplay("environment")).toEqual({ label: "Set by the environment", tone: "success" });
  });
});

describe("what a credential's row offers", () => {
  it("is a field to save a first value when none is set", () => {
    expect(credentialOffer({ source: "not_set", stored: null })).toEqual({
      field: "save",
      removable: false,
      storedIsIgnored: false,
    });
  });

  it("is a field to replace the stored value, and its removal", () => {
    expect(credentialOffer({ source: "stored", stored: STORED })).toEqual({
      field: "replace",
      removable: true,
      storedIsIgnored: false,
    });
  });

  it("is nothing when the environment sets it and nothing is stored", () => {
    expect(credentialOffer({ source: "environment", stored: null })).toEqual({
      field: null,
      removable: false,
      storedIsIgnored: false,
    });
  });

  it("is the removal of a stored value the environment overrides, and no field", () => {
    expect(credentialOffer({ source: "environment", stored: STORED })).toEqual({
      field: null,
      removable: true,
      storedIsIgnored: true,
    });
  });
});
