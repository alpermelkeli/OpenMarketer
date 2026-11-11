import { describe, expect, it } from "vitest";

import { credentialLabel } from "./credential-labels";

const named = (name: string, environment_variable = name.toUpperCase()) => ({ name, environment_variable });

describe("the words for a credential", () => {
  it("name the model provider's key and the repository tokens", () => {
    expect(credentialLabel(named("openrouter_api_key")).label).toBe("OpenRouter API key");
    expect(credentialLabel(named("github_token")).label).toBe("GitHub token");
    expect(credentialLabel(named("gitlab_token")).label).toBe("GitLab token");
  });

  it("name the X app's two credentials and what each is in the developer portal", () => {
    expect(credentialLabel(named("x_api_key")).label).toBe("X API key");
    expect(credentialLabel(named("x_api_key")).purpose).toContain("Consumer Key of your X app");
    expect(credentialLabel(named("x_api_key")).purpose).toContain("OAuth 1.0 Keys");
    expect(credentialLabel(named("x_api_secret")).label).toBe("X API secret");
    expect(credentialLabel(named("x_api_secret")).purpose).toContain("Consumer Secret of your X app");
  });

  it("say what the X app needs set up: its permission, its callback URL and the dashboard's address", () => {
    const setup = credentialLabel(named("x_api_key")).setup ?? "";
    expect(setup).toContain("Read and write");
    expect(setup).toContain("/connections/x/callback");
    expect(setup).toContain("`DASHBOARD_URL`");
    expect(setup).toContain("127.0.0.1");
    expect(setup).toContain("rather than at localhost");
  });

  it("have no setup to describe for the other credentials", () => {
    for (const name of ["openrouter_api_key", "github_token", "gitlab_token", "x_api_secret", "something_new"]) {
      expect(credentialLabel(named(name)).setup).toBeUndefined();
    }
  });

  it("say where a repository token is sent", () => {
    expect(credentialLabel(named("github_token")).purpose).toContain("only to github.com");
    expect(credentialLabel(named("gitlab_token")).purpose).toContain("`GITLAB_HOST`");
  });

  it("say what stops working without each one", () => {
    expect(credentialLabel(named("openrouter_api_key")).withoutIt).toContain("Analyses fail");
    expect(credentialLabel(named("github_token")).withoutIt).toContain("Private repositories on github.com");
    expect(credentialLabel(named("x_api_key")).withoutIt).toContain("connect an X account");
    expect(credentialLabel(named("x_api_secret")).withoutIt).toContain("connect an X account");
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
