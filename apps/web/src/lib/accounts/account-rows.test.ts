import { describe, expect, it } from "vitest";

import type { ConnectablePlatform, ConnectedAccount, SocialAccounts } from "@/lib/api/types";

import {
  accountInitials,
  accountPicture,
  accountRows,
  authorizeDestination,
  blockerExplanation,
  missingCredentialsInWords,
} from "./account-rows";

const PROJECT = "8ce5e2bb-6e54-49b4-9ddc-b96f20ec1109";

const READY_X: ConnectablePlatform = { platform: "x", name: "X", ready: true, missing_credentials: [] };
const UNREADY_X: ConnectablePlatform = {
  platform: "x",
  name: "X",
  ready: false,
  missing_credentials: [
    { name: "x_api_key", environment_variable: "X_API_KEY" },
    { name: "x_api_secret", environment_variable: "X_API_SECRET" },
  ],
};

const ACCOUNT: ConnectedAccount = {
  project_id: PROJECT,
  platform: "x",
  account_id: "1234",
  username: "openmarketer",
  display_name: "Open Marketer",
  picture_url: "https://pbs.twimg.com/profile_images/1/a.jpg",
  connected_by: "0b0e6c6e-1111-4222-8333-444455556666",
  connected_at: "2026-11-10T12:00:00Z",
  updated_at: "2026-11-10T12:00:00Z",
};

function list(overrides: Partial<SocialAccounts>): SocialAccounts {
  return { accounts: [], platforms: [READY_X], can_connect: true, ...overrides };
}

describe("the accounts screen's rows", () => {
  it("are one per platform the API lists, in its order", () => {
    expect(accountRows(list({})).map((row) => row.platform.platform)).toEqual(["x"]);
    expect(accountRows(list({ platforms: [] }))).toEqual([]);
  });

  it("show a ready platform with no account as one that can be connected", () => {
    const [row] = accountRows(list({}));
    expect(row.account).toBeNull();
    expect(row.blockers).toEqual([]);
  });

  it("show the account connected on a platform", () => {
    const [row] = accountRows(list({ accounts: [ACCOUNT] }));
    expect(row.account).toEqual(ACCOUNT);
    expect(row.blockers).toEqual([]);
  });

  it("close the connection while the platform's app is not set, naming what is missing in words", () => {
    const [row] = accountRows(list({ platforms: [UNREADY_X] }));
    expect(row.blockers).toEqual([{ kind: "app_not_set", missing: ["X API key", "X API secret"] }]);
  });

  it("close the connection while the API has no encryption key", () => {
    const [row] = accountRows(list({ can_connect: false }));
    expect(row.blockers).toEqual([{ kind: "encryption_key_missing" }]);
  });

  it("give every reason when there are several, the encryption key first", () => {
    const [row] = accountRows(list({ can_connect: false, platforms: [UNREADY_X] }));
    expect(row.blockers.map((blocker) => blocker.kind)).toEqual(["encryption_key_missing", "app_not_set"]);
  });

  it("keep the connection closed for another account too, while the reason holds", () => {
    const [row] = accountRows(list({ accounts: [ACCOUNT], can_connect: false }));
    expect(row.account).toEqual(ACCOUNT);
    expect(row.blockers).toHaveLength(1);
  });
});

describe("why a connection is closed", () => {
  it("names the missing credentials and where to set them", () => {
    const text = blockerExplanation({ kind: "app_not_set", missing: ["X API key", "X API secret"] }, "X");
    expect(text).toBe(
      "The X API key and the X API secret of the installation's X app are not set. Set them in Settings, under Credentials, where what the app needs is described too.",
    );
    expect(blockerExplanation({ kind: "app_not_set", missing: ["X API key"] }, "X")).toContain("X API key of the installation's X app is not set. Set it");
  });

  it("names the encryption key the API lacks", () => {
    expect(blockerExplanation({ kind: "encryption_key_missing" }, "X")).toContain("`ENCRYPTION_KEY`");
  });

  it("lists names as a sentence does", () => {
    expect(missingCredentialsInWords([])).toBe("some credentials");
    expect(missingCredentialsInWords(["A"])).toBe("the A");
    expect(missingCredentialsInWords(["A", "B", "C"])).toBe("the A, the B and the C");
  });
});

describe("an account's picture and initials", () => {
  it("show a picture only from an https address", () => {
    expect(accountPicture(ACCOUNT)).toBe(ACCOUNT.picture_url);
    expect(accountPicture({ picture_url: null })).toBeNull();
    expect(accountPicture({ picture_url: "http://pbs.twimg.com/a.jpg" })).toBeNull();
    expect(accountPicture({ picture_url: "javascript:alert(1)" })).toBeNull();
    expect(accountPicture({ picture_url: "not a url" })).toBeNull();
  });

  it("take initials from the display name, else from the username", () => {
    expect(accountInitials({ display_name: "Open Marketer", username: "om" })).toBe("OM");
    expect(accountInitials({ display_name: "Ada", username: "om" })).toBe("A");
    expect(accountInitials({ display_name: "  ", username: "openmarketer" })).toBe("O");
    expect(accountInitials({ display_name: "🚀 Rocket Ship", username: "r" })).toBe("RS");
    expect(accountInitials({ display_name: "", username: "" })).toBe("?");
  });
});

describe("where the browser is sent to agree", () => {
  it("is the API's answer when it is an https address", () => {
    const url = "https://api.x.com/oauth/authorize?oauth_token=abc";
    expect(authorizeDestination(url)).toBe(url);
  });

  it("is nowhere for any other address", () => {
    for (const url of ["javascript:alert(1)", "http://api.x.com/oauth/authorize", "/elsewhere", ""]) {
      expect(authorizeDestination(url)).toBeNull();
    }
  });
});
