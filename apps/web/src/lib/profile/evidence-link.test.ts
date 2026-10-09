import { describe, expect, it } from "vitest";

import { commitOrNull, evidenceLink, httpsUrlOrNull } from "./evidence-link";

const github = "https://github.com/alpermelkeli/Memoria";

describe("a link to evidence", () => {
  it("points at the file and lines on GitHub's default branch", () => {
    expect(evidenceLink(github, { file: "app/src/Main.kt", lines: "12-88" })).toEqual({
      href: "https://github.com/alpermelkeli/Memoria/blob/HEAD/app/src/Main.kt#L12-L88",
      host: "github.com",
      pinned: false,
    });
  });

  it("uses each host's own form for a file and a line range", () => {
    const evidence = { file: "README.md", lines: "3-9" };
    expect(evidenceLink("https://gitlab.com/group/tool", evidence)?.href).toBe(
      "https://gitlab.com/group/tool/-/blob/HEAD/README.md#L3-9",
    );
    expect(evidenceLink("https://bitbucket.org/team/tool", evidence)?.href).toBe(
      "https://bitbucket.org/team/tool/src/HEAD/README.md#lines-3:9",
    );
  });

  it("marks a single line, and no line when none is given", () => {
    expect(evidenceLink(github, { file: "README.md", lines: "7" })?.href).toMatch(/#L7$/);
    expect(evidenceLink(github, { file: "README.md", lines: null })?.href).toMatch(/README\.md$/);
    expect(evidenceLink(github, { file: "README.md" })?.href).toMatch(/README\.md$/);
  });

  it("accepts a clone URL ending in .git or a slash", () => {
    expect(evidenceLink(`${github}.git`, { file: "a.md" })?.href).toBe(
      "https://github.com/alpermelkeli/Memoria/blob/HEAD/a.md",
    );
    expect(evidenceLink(`${github}/`, { file: "a.md" })).not.toBeNull();
  });

  it("encodes a path so it cannot add a query, a fragment or markup", () => {
    const link = evidenceLink(github, { file: 'docs/a b?x=1#y/"><script>.md', lines: "1" });
    expect(link?.href).toBe(
      "https://github.com/alpermelkeli/Memoria/blob/HEAD/docs/a%20b%3Fx%3D1%23y/%22%3E%3Cscript%3E.md#L1",
    );
  });

  it("is not built for a path that leaves the repository", () => {
    for (const file of ["../../other/repo", "/etc/passwd", "a/../../b", "a//b", "a\\..\\b", ""]) {
      expect(evidenceLink(github, { file })).toBeNull();
    }
  });

  it("ignores lines that are not a range", () => {
    const link = evidenceLink(github, { file: "a.md", lines: "1-2&x=<script>" });
    expect(link?.href).toBe("https://github.com/alpermelkeli/Memoria/blob/HEAD/a.md");
    expect(evidenceLink(github, { file: "a.md", lines: "9-2" })?.href).not.toContain("#");
  });

  it("is not built for a host that is not known", () => {
    for (const url of [
      "https://git.example.com/owner/repo",
      "https://github.com.evil.example/owner/repo",
      "https://evil.example/github.com/owner/repo",
      "https://github.com:8443/owner/repo",
    ]) {
      expect(evidenceLink(url, { file: "a.md" })).toBeNull();
    }
  });

  it("is not built for a repository URL that is not plain https owner/name", () => {
    for (const url of [
      "http://github.com/owner/repo",
      "javascript:alert(1)//github.com/owner/repo",
      "https://user:token@github.com/owner/repo",
      "https://github.com/owner",
      "https://github.com/owner/repo/tree/main",
      "https://github.com/owner/..",
      "not a url",
    ]) {
      expect(evidenceLink(url, { file: "a.md" })).toBeNull();
    }
  });
});

describe("a link to evidence at the analysed commit", () => {
  const commit = "ecedeaa63b039827bbd703bf604c801395e0b507";
  const evidence = { file: "docs/guide.md", lines: "3-9" };

  it("is a permalink on each known host", () => {
    expect(evidenceLink(github, evidence, commit)).toEqual({
      href: `https://github.com/alpermelkeli/Memoria/blob/${commit}/docs/guide.md#L3-L9`,
      host: "github.com",
      pinned: true,
    });
    expect(evidenceLink("https://gitlab.com/group/tool", evidence, commit)?.href).toBe(
      `https://gitlab.com/group/tool/-/blob/${commit}/docs/guide.md#L3-9`,
    );
    expect(evidenceLink("https://bitbucket.org/team/tool", evidence, commit)?.href).toBe(
      `https://bitbucket.org/team/tool/src/${commit}/docs/guide.md#lines-3:9`,
    );
  });

  it("falls back to the default branch when no commit was recorded", () => {
    const link = evidenceLink(github, evidence, null);
    expect(link).toMatchObject({ pinned: false });
    expect(link?.href).toContain("/blob/HEAD/");
  });

  it("falls back to the default branch for anything that is not a full commit id", () => {
    for (const value of ["main", "ecedeaa", "../../settings", "HEAD/../..", `${commit}/x`, `${commit}?a=b`, ""]) {
      expect(commitOrNull(value)).toBeNull();
      expect(evidenceLink(github, evidence, value)?.href).toContain("/blob/HEAD/");
    }
  });

  it("accepts a SHA-256 commit id and writes it in lower case", () => {
    const sha256 = "A".repeat(64);
    expect(commitOrNull(sha256)).toBe("a".repeat(64));
  });
});

describe("a repository URL used as a link", () => {
  it("is kept when it is https", () => {
    expect(httpsUrlOrNull("https://git.example.com/a/b")).toBe("https://git.example.com/a/b");
  });

  it("is dropped for any other scheme, or with credentials", () => {
    for (const url of ["http://a.example/b", "javascript:alert(1)", "data:text/html,x", "https://u:p@a.example", ""]) {
      expect(httpsUrlOrNull(url)).toBeNull();
    }
  });
});
