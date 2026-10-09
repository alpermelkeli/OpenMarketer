/**
 * A link from a piece of evidence to the file on the repository's host.
 *
 * Both inputs are untrusted: the repository URL was typed by someone and the
 * evidence path was written by a model reading the repository. A link is built
 * only for a known host, from a fixed https origin and encoded path segments, so
 * neither input can choose the scheme, the host or anything outside the
 * repository's own pages.
 *
 * The API does not say which commit was analysed, so the link points at the
 * default branch as it is today (`HEAD`), not at the lines the analyzer read.
 */

import type { Evidence } from "@/lib/api/types";

export type EvidenceLink = { href: string; host: string };

type HostFormat = {
  filePath: (owner: string, repo: string, file: string) => string;
  lines: (start: number, end: number) => string;
};

const HOSTS: Record<string, HostFormat> = {
  "github.com": {
    filePath: (owner, repo, file) => `/${owner}/${repo}/blob/HEAD/${file}`,
    lines: (start, end) => (start === end ? `L${start}` : `L${start}-L${end}`),
  },
  "gitlab.com": {
    filePath: (owner, repo, file) => `/${owner}/${repo}/-/blob/HEAD/${file}`,
    lines: (start, end) => (start === end ? `L${start}` : `L${start}-${end}`),
  },
  "bitbucket.org": {
    filePath: (owner, repo, file) => `/${owner}/${repo}/src/HEAD/${file}`,
    lines: (start, end) => (start === end ? `lines-${start}` : `lines-${start}:${end}`),
  },
};

const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const LINES = /^(\d{1,9})(?:-(\d{1,9}))?$/;

/** The repository URL if it is safe to use as a link target: https and nothing else. */
export function httpsUrlOrNull(value: string): string | null {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.username !== "" || url.password !== "") return null;
  return url.href;
}

export function evidenceLink(repositoryUrl: string, evidence: Evidence): EvidenceLink | null {
  const repository = knownRepository(repositoryUrl);
  const file = encodedRelativePath(evidence.file);
  if (repository === null || file === null) return null;

  const { host, owner, repo } = repository;
  const format = HOSTS[host];
  const url = new URL(format.filePath(owner, repo, file), `https://${host}`);
  const range = lineRange(evidence.lines);
  if (range !== null) url.hash = format.lines(range.start, range.end);
  return { href: url.href, host };
}

function knownRepository(repositoryUrl: string): { host: string; owner: string; repo: string } | null {
  const safe = httpsUrlOrNull(repositoryUrl);
  if (safe === null) return null;
  const url = new URL(safe);
  if (!Object.hasOwn(HOSTS, url.hostname) || url.port !== "") return null;

  const parts = url.pathname.split("/").filter((part) => part !== "");
  if (parts.length !== 2) return null;
  const [owner, repoWithSuffix] = parts;
  const repo = repoWithSuffix.replace(/\.git$/, "");
  if (!NAME.test(owner) || !NAME.test(repo) || repo === "." || repo === "..") return null;
  return { host: url.hostname, owner, repo };
}

function encodedRelativePath(file: string): string | null {
  if (file.startsWith("/") || file.includes("\\")) return null;
  const segments = file.split("/");
  const usable = segments.every((s) => s !== "" && s !== "." && s !== "..");
  return usable ? segments.map(encodeURIComponent).join("/") : null;
}

function lineRange(lines: string | null | undefined): { start: number; end: number } | null {
  const match = LINES.exec(lines ?? "");
  if (match === null) return null;
  const start = Number(match[1]);
  const end = Number(match[2] ?? match[1]);
  return start >= 1 && end >= start ? { start, end } : null;
}
