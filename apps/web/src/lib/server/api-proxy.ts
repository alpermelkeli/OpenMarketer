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
 * `ALLOWED_ROUTES` exist, and the upstream URL is built from validated parts, never
 * from the incoming URL.
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
};

function route(method: Method, template: keyof paths): AllowedRoute {
  const pattern = template
    .replace(/\{(project_id|run_id)\}/g, UUID)
    .replace(/\{version\}/g, VERSION);
  return { method, template, pattern: new RegExp(`^${pattern}$`) };
}

/** Every API route the dashboard uses. Nothing else is forwarded. */
export const ALLOWED_ROUTES: readonly AllowedRoute[] = [
  route("POST", "/v1/projects"),
  route("POST", "/v1/projects/{project_id}/analyses"),
  route("GET", "/v1/projects/{project_id}/analyses/{run_id}"),
  route("GET", "/v1/projects/{project_id}/profile/draft"),
  route("GET", "/v1/projects/{project_id}/profile/approved"),
  route("POST", "/v1/projects/{project_id}/profile/versions/{version}/edits"),
  route("POST", "/v1/projects/{project_id}/profile/versions/{version}/approval"),
];

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

/** The API path for an allowed request; refuses every other method and path. */
export function allowedApiPath(method: string, pathSegments: readonly string[]): string {
  const path = `/v1/${pathSegments.join("/")}`;
  const allowed = ALLOWED_ROUTES.some((r) => r.method === method && r.pattern.test(path));
  if (!allowed) {
    throw new ProxyRefusal(
      404,
      "proxy_route_not_allowed",
      "the dashboard does not forward this method and path",
    );
  }
  return path;
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
 * Only the method, the validated path and the JSON body travel upstream: no cookie,
 * no query string and no header of the browser's.
 */
export async function proxyToApi(
  request: Request,
  pathSegments: readonly string[],
  configuredApiUrl: string | undefined,
  send: typeof fetch = fetch,
): Promise<Response> {
  try {
    assertFromDashboard(request.method, request.headers);
    const path = allowedApiPath(request.method, pathSegments);
    const body = await readJsonBody(request);
    const target = new URL(path, apiBaseUrl(configuredApiUrl));
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
