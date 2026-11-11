import { describe, expect, it } from "vitest";

import { ApiError, type ApiErrorCode } from "@/lib/api/api-error";

import { callbackProblem, connectProblem, disconnectProblem } from "./connection-problems";

const error = (code: ApiErrorCode, status = 409, message = "the API's own words") =>
  new ApiError(status, code, message);

describe("why a connection could not be started", () => {
  it("sends the person to the credentials when the app is not set or its credential cannot be read", () => {
    expect(connectProblem(error("platform_app_not_set"), "X")).toContain("Settings, under Credentials");
    expect(connectProblem(error("credential_unreadable"), "X")).toContain("Store it again");
  });

  it("names the encryption key the API lacks", () => {
    expect(connectProblem(error("encryption_key_missing"), "X")).toContain("`ENCRYPTION_KEY`");
  });

  it("says what a refusal of the platform most often means", () => {
    expect(connectProblem(error("authorization_refused", 400), "X")).toMatch(/^X refused .*callback URL/);
  });

  it("says when the platform cannot be reached", () => {
    expect(connectProblem(error("platform_unavailable", 502), "X")).toBe("X could not be reached. Try again in a moment.");
  });

  it("shows the API's message for anything else", () => {
    expect(connectProblem(error("internal_error", 500, "boom"), "X")).toContain("did not expect");
    expect(connectProblem(error("unexpected_response", 418, "odd"), "X")).toBe("odd");
  });
});

describe("why an account could not be disconnected", () => {
  it("says when there was no account any more", () => {
    expect(disconnectProblem(error("social_account_not_found", 404))).toContain("no account connected there");
  });
});

describe("why the platform's answer could not be finished", () => {
  it("says a connection that is unknown or used already must be started again", () => {
    const problem = callbackProblem(error("connection_not_found", 404));
    expect(problem.title).toBe("This connection is unknown or was already used");
    expect(problem.message).toContain("Start again from the project's Accounts page.");
  });

  it("says a connection that took too long must be started again", () => {
    const problem = callbackProblem(error("connection_expired", 410));
    expect(problem.title).toBe("The connection took too long");
    expect(problem.message).toContain("Start again from the project's Accounts page.");
  });

  it("has words for every refusal the callback documents", () => {
    for (const code of [
      "connection_secret_unreadable",
      "authorization_refused",
      "platform_unavailable",
      "platform_app_not_set",
      "credential_unreadable",
      "encryption_key_missing",
    ] as const) {
      expect(callbackProblem(error(code)).message).not.toBe("the API's own words");
    }
  });

  it("shows the API's message for anything else", () => {
    expect(callbackProblem(error("unexpected_response", 418, "odd"))).toEqual({
      title: "The connection was not finished",
      message: "odd",
    });
  });
});
