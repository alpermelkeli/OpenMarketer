/**
 * The rules of the dashboard's proxy to the OpenMarketer API.
 *
 * The API has no login: it trusts a request that comes from this machine, and it
 * uses the browser's `Origin` header to tell the local dashboard from any other
 * website open in the same browser. A server-side proxy hides that header from the
 * API, so the same decision has to be made here, before anything is forwarded.
 *
 * This module decides and builds; it does no I/O except through the `fetch` it is
 * given. It deliberately is not a general proxy: only the routes listed in
 * `ALLOWED_ROUTES` exist, the only query parameters are the two that page a list,
 * and the upstream URL is built from validated parts, never from the incoming URL.
 */

import type { paths } from "@/lib/api/schema";
import type { ProxyErrorCode, ProxyProblem } from "@/lib/api/proxy-problem";

export const DEFAULT_API_URL = "http://127.0.0.1:8000";

/** Larger than any profile the analyzer produces; small enough to refuse abuse. */
export const MAX_BODY_BYTES = 1_000_000;

const UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";
const VERSION = "[1-9][0-9]{0,8}";

// The whole of a Host header that names this machine, as the API itself matches it.
const LOCAL_HOST = /^(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$/i;

type Method = "GET" | "POST";

type AllowedRoute = {
  method: Method;
  /** The API path this route forwards to, kept in step with the contract by its type. */
  template: keyof paths;
  pattern: RegExp;
  /** A list that is read page by page: `limit` and `cursor` are forwarded. */
  paged: boolean;
};

function route(method: Method, template: keyof paths, paged = false): AllowedRoute {
  const pattern = template
    .replace(/\{(project_id|run_id)\}/g, UUID)
    .replace(/\{version\}/g, VERSION);
  return { method, template, pattern: new RegExp(`^${pattern}$`), paged };
}

const PAGED = true;

/** Every API route the dashboard uses. Nothing else is forwarded. */
export const ALLOWED_ROUTES: readonly AllowedRoute[] = [
  route("GET", "/v1/projects", PAGED),
  route("POST", "/v1/projects"),
  route("GET", "/v1/projects/{project_id}"),
  route("GET", "/v1/projects/{project_id}/analyses", PAGED),
  route("POST", "/v1/projects/{project_id}/analyses"),
  route("GET", "/v1/projects/{project_id}/analyses/{run_id}"),
  route("GET", "/v1/projects/{project_id}/profile/versions", PAGED),
  route("GET", "/v1/projects/{project_id}/profile/versions/{version}"),
  route("POST", "/v1/projects/{project_id}/profile/versions/{version}/edits"),
  route("POST", "/v1/projects/{project_id}/profile/versions/{version}/approval"),
];

// What a page of a list may be asked for, as the contract has it: 1 to 100 items,
// and the API's own opaque cursor (URL-safe base64, at most 200 characters).
const MAX_PAGE_SIZE = 100;
const LIMIT = /^[1-9][0-9]{0,2}$/;
const CURSOR = /^[A-Za-z0-9_-]{1,200}$/;

export class ProxyRefusal extends Error {
  constructor(
    readonly status: number,
    readonly code: ProxyErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "ProxyRefusal";
  }

  toResponse(): Response {
    const problem: ProxyProblem = { code: this.code, message: this.message };
    return jsonResponse(this.status, JSON.stringify(problem));
  }
}

/**
 * Refuse a request that does not provably come from the dashboard's own page.
 *
 * - `Host` must name this machine, so a foreign name pointed at 127.0.0.1 (DNS
 *   rebinding) is not served.
 * - `Sec-Fetch-Site`, when the browser sends it, must say the request was made by a
 *   page of this origin (or, for a read, typed into the address bar).
 * - `Origin`, when present, must be this origin. A request that changes something
 *   must carry it: browsers always send it with a POST, so its absence means the
 *   caller is not the dashboard.
 */
export function assertFromDashboard(method: string, headers: Headers): void {
  const host = headers.get("host");
  if (host === null || !LOCAL_HOST.test(host)) {
    throw refused("the dashboard serves only requests addressed to this machine");
  }

  const changesState = method !== "GET";
  const fetchSite = headers.get("sec-fetch-site");
  const fetchSiteAllowed = changesState ? ["same-origin"] : ["same-origin", "none"];
  if (fetchSite !== null && !fetchSiteAllowed.includes(fetchSite)) {
    throw refused("the request was made by another site");
  }

  const origin = headers.get("origin");
  if (origin === null) {
    if (changesState) throw refused("a request that changes something must carry an Origin");
    return;
  }
  if (origin !== `http://${host}` && origin !== `https://${host}`) {
    throw refused("the request was made by another site");
  }
}

/**
 * The API path and query for an allowed request; refuses every other method and path.
 *
 * A query is refused rather than dropped, so a caller learns that what it sent had
 * no effect: only a paged list takes one, only `limit` and `cursor`, each once and
 * each in the form the API issues.
 */
export function allowedApiTarget(
  method: string,
  pathSegments: readonly string[],
  query: URLSearchParams,
): string {
  const path = `/v1/${pathSegments.join("/")}`;
  const matched = ALLOWED_ROUTES.find((r) => r.method === method && r.pattern.test(path));
  if (matched === undefined) {
    throw new ProxyRefusal(
      404,
      "proxy_route_not_allowed",
      "the dashboard does not forward this method and path",
    );
  }
  const forwarded = pageQuery(query, matched.paged);
  return forwarded === "" ? path : `${path}?${forwarded}`;
}

function pageQuery(query: URLSearchParams, paged: boolean): string {
  const forwarded = new URLSearchParams();
  for (const name of new Set(query.keys())) {
    const values = query.getAll(name);
    const valid =
      paged &&
      values.length === 1 &&
      ((name === "limit" && LIMIT.test(values[0]) && Number(values[0]) <= MAX_PAGE_SIZE) ||
        (name === "cursor" && CURSOR.test(values[0])));
    if (!valid) {
      throw new ProxyRefusal(
        400,
        "proxy_query_rejected",
        paged
          ? "a list takes only `limit` (1 to 100) and `cursor` (as the API issued it), each once"
          : "this route takes no query parameters",
      );
    }
    forwarded.set(name, values[0]);
  }
  return forwarded.toString();
}

/** The API's base URL from server configuration: an http(s) origin and nothing more. */
export function apiBaseUrl(configured: string | undefined): URL {
  const url = parseUrl(configured?.trim() || DEFAULT_API_URL);
  const isOrigin =
    url !== null &&
    (url.protocol === "http:" || url.protocol === "https:") &&
    url.username === "" &&
    url.password === "" &&
    url.pathname === "/" &&
    url.search === "" &&
    url.hash === "";
  if (!isOrigin) {
    throw new ProxyRefusal(
      500,
      "proxy_misconfigured",
      "OPENMARKETER_API_URL must be an http(s) origin such as http://127.0.0.1:8000",
    );
  }
  return url;
}

/** The JSON body to forward, or none. Anything that is not JSON, or is too large, is refused. */
export async function readJsonBody(request: Request): Promise<string | undefined> {
  if (request.method === "GET") return undefined;
  const body = await request.text();
  if (body === "") return undefined;
  const mediaType = request.headers.get("content-type")?.split(";")[0].trim().toLowerCase();
  if (mediaType !== "application/json") {
    throw new ProxyRefusal(415, "proxy_body_rejected", "a request body must be application/json");
  }
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) {
    throw new ProxyRefusal(413, "proxy_body_rejected", "the request body is too large");
  }
  return body;
}

/**
 * Check a request and forward it to the API, or answer with the refusal.
 *
 * Only the method, the validated path, a list's page parameters and the JSON body
 * travel upstream: no cookie, no other query and no header of the browser's.
 */
export async function proxyToApi(
  request: Request,
  pathSegments: readonly string[],
  configuredApiUrl: string | undefined,
  send: typeof fetch = fetch,
): Promise<Response> {
  try {
    assertFromDashboard(request.method, request.headers);
    const query = new URL(request.url).searchParams;
    const pathAndQuery = allowedApiTarget(request.method, pathSegments, query);
    const body = await readJsonBody(request);
    const target = new URL(pathAndQuery, apiBaseUrl(configuredApiUrl));
    return await forward(send, target, request.method, body);
  } catch (error) {
    if (error instanceof ProxyRefusal) return error.toResponse();
    throw error;
  }
}

async function forward(
  send: typeof fetch,
  target: URL,
  method: string,
  body: string | undefined,
): Promise<Response> {
  let upstream: Response;
  try {
    upstream = await send(target, {
      method,
      body,
      headers: {
        accept: "application/json",
        ...(body === undefined ? {} : { "content-type": "application/json" }),
      },
      redirect: "manual",
      cache: "no-store",
    });
  } catch {
    throw new ProxyRefusal(
      502,
      "api_unreachable",
      "the OpenMarketer API did not answer; start it with `make api`",
    );
  }
  const text = await upstream.text();
  // A status that may not carry a body (204, 304) cannot be given one.
  return jsonResponse(upstream.status, text === "" ? null : text);
}

function refused(message: string): ProxyRefusal {
  return new ProxyRefusal(403, "proxy_request_refused", message);
}

function parseUrl(value: string): URL | null {
  try {
    return new URL(value);
  } catch {
    return null;
  }
}

function jsonResponse(status: number, body: string | null): Response {
  return new Response(body, {
    status,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}
