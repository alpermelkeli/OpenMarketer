import { describe, expect, it } from "vitest";

import {
  chatHref,
  chatIdOfPath,
  projectIdOfPath,
  projectPageHref,
  projectPageOfPath,
  shortRepositoryName,
} from "./project-navigation";

const ID = "7b755025-304e-457a-99f6-9d8245b7a35a";

describe("which project an address is inside", () => {
  it("is the project of any of its pages", () => {
    expect(projectIdOfPath(`/projects/${ID}`)).toBe(ID);
    expect(projectIdOfPath(`/projects/${ID}/approvals`)).toBe(ID);
  });

  it("is none outside the projects", () => {
    expect(projectIdOfPath("/")).toBeNull();
    expect(projectIdOfPath("/settings/models")).toBeNull();
  });

  it("is none for a segment that is not a project's ID", () => {
    expect(projectIdOfPath("/projects/not-an-id/profile")).toBeNull();
  });
});

describe("which page of the project an address shows", () => {
  it("is the chat or one of the project's pages", () => {
    expect(projectPageOfPath(`/projects/${ID}/chat`)).toBe("chat");
    expect(projectPageOfPath(`/projects/${ID}/profile`)).toBe("profile");
    expect(projectPageOfPath(`/projects/${ID}/analyses`)).toBe("analyses");
    expect(projectPageOfPath(`/projects/${ID}/approvals`)).toBe("approvals");
  });

  it("is none for the project's own address and an unknown page", () => {
    expect(projectPageOfPath(`/projects/${ID}`)).toBeNull();
    expect(projectPageOfPath(`/projects/${ID}/elsewhere`)).toBeNull();
  });

  it("is reached at the project's address of that page", () => {
    expect(projectPageHref(ID, "chat")).toBe(`/projects/${ID}/chat`);
  });
});

describe("the repository's name under the project", () => {
  it("leaves out the scheme and the host of GitHub and GitLab", () => {
    expect(shortRepositoryName("https://github.com/owner/name")).toBe("owner/name");
    expect(shortRepositoryName("https://gitlab.com/group/name.git")).toBe("group/name");
  });

  it("keeps any other address whole", () => {
    expect(shortRepositoryName("https://git.example.org/owner/name")).toBe("https://git.example.org/owner/name");
    expect(shortRepositoryName("/home/me/repo")).toBe("/home/me/repo");
  });
});

describe("which chat an address shows", () => {
  const CHAT = "0f3c9a52-0d1e-4f54-8a55-2c4f1b7d9e10";

  it("is the chat under the project's chat page", () => {
    expect(chatIdOfPath(chatHref(ID, CHAT))).toBe(CHAT);
  });

  it("is none for a new chat", () => {
    expect(chatIdOfPath(`/projects/${ID}/chat`)).toBeNull();
  });

  it("belongs to the chat page", () => {
    expect(projectPageOfPath(chatHref(ID, CHAT))).toBe("chat");
  });
});
