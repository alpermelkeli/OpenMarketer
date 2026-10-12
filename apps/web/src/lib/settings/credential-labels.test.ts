import { describe, expect, it } from "vitest";

import { credentialLabel } from "./credential-labels";

const named = (name: string, environment_variable = name.toUpperCase()) => ({ name, environment_variable });

describe("the words for a credential", () => {
  it("name the three credentials the API has today", () => {
    expect(credentialLabel(named("openrouter_api_key")).label).toBe("OpenRouter API key");
    expect(credentialLabel(named("github_token")).label).toBe("GitHub token");
    expect(credentialLabel(named("gitlab_token")).label).toBe("GitLab token");
  });

  it("say where a repository token is sent", () => {
    expect(credentialLabel(named("github_token")).purpose).toContain("only to github.com");
    expect(credentialLabel(named("gitlab_token")).purpose).toContain("`GITLAB_HOST`");
  });

  it("say what stops working without each one", () => {
    expect(credentialLabel(named("openrouter_api_key")).withoutIt).toContain("Analyses fail");
    expect(credentialLabel(named("github_token")).withoutIt).toContain("Private repositories on github.com");
  });

  it("call a credential the dashboard does not know by its name and its environment variable", () => {
    const label = credentialLabel(named("bitbucket_token"));
    expect(label.label).toBe("bitbucket_token");
    expect(label.purpose).toContain("`BITBUCKET_TOKEN`");
    expect(label.withoutIt).not.toBe("");
  });

  it("do not take a name for one of an object's own members", () => {
    for (const name of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(credentialLabel(named(name)).label).toBe(name);
    }
  });

  it("leave out an environment variable that does not have the form of a name", () => {
    for (const variable of ["", "A`B", "lower_case", "WITH SPACE", "<b>X</b>"]) {
      const { purpose } = credentialLabel(named("something_new", variable));
      expect(purpose).toBe("This dashboard has no description of this credential yet.");
    }
  });
});
