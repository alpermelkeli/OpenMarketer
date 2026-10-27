import { describe, expect, it } from "vitest";

import {
  chatHref,
  chatIdOfPath,
  projectIdOfPath,
  projectPageHref,
  projectPageOfPath,
  PROJECT_PAGES,
  researchHref,
  proposalAnchor,
  proposalHref,
  shortRepositoryName,
  studioHref,
  writingHref,
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
    expect(projectPageOfPath(`/projects/${ID}/research`)).toBe("research");
    expect(projectPageOfPath(`/projects/${ID}/studio`)).toBe("studio");
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

describe("the address of a research run", () => {
  const RUN = "6d5e4f3a-2b1c-4d5e-8f90-1a2b3c4d5e6f";

  it("sits under the project's research page", () => {
    expect(researchHref(ID, RUN)).toBe(`/projects/${ID}/research/${RUN}`);
  });

  it("belongs to the research page", () => {
    expect(projectPageOfPath(researchHref(ID, RUN))).toBe("research");
  });
});

describe("the studio", () => {
  const RUN = "6d5e4f3a-2b1c-4d5e-8f90-1a2b3c4d5e6f";

  it("is listed after the research and before the approvals", () => {
    const pages = PROJECT_PAGES.map((entry) => entry.page);
    expect(pages.indexOf("studio")).toBe(pages.indexOf("research") + 1);
    expect(pages.indexOf("approvals")).toBeGreaterThan(pages.indexOf("studio"));
    expect(PROJECT_PAGES.find((entry) => entry.page === "studio")?.label).toBe("Studio");
  });

  it("has an address for each of its runs, which belongs to the studio page", () => {
    expect(studioHref(ID, RUN)).toBe(`/projects/${ID}/studio/${RUN}`);
    expect(projectPageOfPath(studioHref(ID, RUN))).toBe("studio");
  });
});

describe("the writing", () => {
  const RUN = "6d5e4f3a-2b1c-4d5e-8f90-1a2b3c4d5e6f";

  it("is listed after the studio and before the approvals", () => {
    const pages = PROJECT_PAGES.map((entry) => entry.page);
    expect(pages.indexOf("writing")).toBe(pages.indexOf("studio") + 1);
    expect(pages.indexOf("approvals")).toBe(pages.indexOf("writing") + 1);
    expect(PROJECT_PAGES.find((entry) => entry.page === "writing")?.label).toBe("Writing");
  });

  it("has an address for each of its runs, which belongs to the writing page", () => {
    expect(writingHref(ID, RUN)).toBe(`/projects/${ID}/writing/${RUN}`);
    expect(projectPageOfPath(writingHref(ID, RUN))).toBe("writing");
  });
});

describe("the address of a proposal", () => {
  const PROPOSAL = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";

  it("points at its card on the approvals page", () => {
    expect(proposalHref(ID, PROPOSAL)).toBe(`/projects/${ID}/approvals#${proposalAnchor(PROPOSAL)}`);
    // A pathname never carries the fragment, so the page is read from what comes before it.
    expect(projectPageOfPath(proposalHref(ID, PROPOSAL).split("#")[0])).toBe("approvals");
  });
});
