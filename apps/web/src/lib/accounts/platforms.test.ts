import { describe, expect, it } from "vitest";

import { accountProfileHref, callbackRequest, isSocialPlatform } from "./platforms";

const query = (text: string) => new URLSearchParams(text);

describe("what X's callback sends on", () => {
  it("is the token as the state and the verifier as the grant when the person agreed", () => {
    expect(callbackRequest("x", query("oauth_token=tok&oauth_verifier=ver"))).toEqual({ state: "tok", grant: "ver" });
  });

  it("is the denied token alone when the person cancelled", () => {
    expect(callbackRequest("x", query("denied=tok"))).toEqual({ state: "tok", grant: null });
  });

  it("is the cancellation when the address holds both", () => {
    expect(callbackRequest("x", query("denied=tok&oauth_token=tok&oauth_verifier=ver"))).toEqual({
      state: "tok",
      grant: null,
    });
  });

  it("is nothing without a whole answer", () => {
    for (const text of ["", "oauth_token=tok", "oauth_verifier=ver", "oauth_token=&oauth_verifier=ver", "denied="]) {
      expect(callbackRequest("x", query(text))).toBeNull();
    }
  });

  it("is nothing when a parameter is given twice", () => {
    expect(callbackRequest("x", query("oauth_token=a&oauth_token=b&oauth_verifier=ver"))).toBeNull();
    expect(callbackRequest("x", query("denied=a&denied=b"))).toBeNull();
  });
});

describe("which platforms the dashboard knows", () => {
  it("is X", () => {
    expect(isSocialPlatform("x")).toBe(true);
  });

  it("is no other slug, nor one of an object's own members", () => {
    for (const value of ["X", "twitter", "", "constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(isSocialPlatform(value)).toBe(false);
    }
  });
});

describe("an account's public profile", () => {
  it("is on the platform's own origin", () => {
    expect(accountProfileHref("x", "openmarketer")).toBe("https://x.com/openmarketer");
  });

  it("encodes the username as one segment", () => {
    expect(accountProfileHref("x", "../a?b#c")).toBe("https://x.com/..%2Fa%3Fb%23c");
  });

  it("is nowhere for an empty username", () => {
    expect(accountProfileHref("x", "")).toBeNull();
  });
});
