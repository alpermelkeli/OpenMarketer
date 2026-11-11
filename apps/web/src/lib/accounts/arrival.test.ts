import { describe, expect, it } from "vitest";

import { arrivalHref, arrivalOf } from "./arrival";

const PROJECT = "8ce5e2bb-6e54-49b4-9ddc-b96f20ec1109";

const queryOf = (href: string) => new URL(href, "http://127.0.0.1:3000").searchParams;

describe("the outcome a connection brings to the accounts screen", () => {
  it("is carried in the address of the project's accounts", () => {
    expect(arrivalHref(PROJECT, { outcome: "connected", platform: "x" })).toBe(
      `/projects/${PROJECT}/accounts?connected=x`,
    );
    expect(arrivalHref(PROJECT, { outcome: "declined", platform: "x" })).toBe(
      `/projects/${PROJECT}/accounts?declined=x`,
    );
  });

  it("is read back as it was written", () => {
    for (const outcome of ["connected", "declined"] as const) {
      expect(arrivalOf(queryOf(arrivalHref(PROJECT, { outcome, platform: "x" })))).toEqual({ outcome, platform: "x" });
    }
  });

  it("is none for an address the callback page did not write", () => {
    for (const text of ["", "connected=twitter", "connected=x&declined=x", "connected=x&connected=x", "other=x"]) {
      expect(arrivalOf(new URLSearchParams(text))).toBeNull();
    }
  });
});
