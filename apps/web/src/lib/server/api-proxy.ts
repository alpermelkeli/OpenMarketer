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
 * `ALLOWED_ROUTES` exist, the only query parameters are the two that page a list
 * and the one that resumes an event stream, and the upstream URL is built
 * from validated parts, never from the incoming URL.
 *
 * Two routes stream: a chat's events and the workspace's, as server-sent events.
 * Their answers are passed through as they arrive instead of being read whole, they
 * are the only routes that take a browser header (`Last-Event-ID`, which `EventSource`
 * sends when it reconnects, checked to be an event id), and the upstream request ends
 * when the browser leaves.
 *
 * Three routes answer with an image: a studio version's, to show in the page or to save,
 * and one the last analysis found in the project's repository. They are reads only. Their body is passed on as bytes, but only when the API says it is
 * a PNG, a JPEG or a WebP image and it is no larger than `MAX_IMAGE_BYTES`; anything else
 * is refused. Of the API's headers only the five an image needs come back, and the type
 * is the one checked, never whatever else the API wrote.
 *
 * A request body can hold a secret (a credential being stored). It is passed on to
 * the API and nowhere else: nothing here logs it or puts it in an answer.
 */

import type { paths } from "@/lib/api/schema";
import type { ProxyErrorCode, ProxyProblem } from "@/lib/api/proxy-problem";

export const DEFAULT_API_URL = "http://127.0.0.1:8000";

/** Larger than any profile the analyzer produces; small enough to refuse abuse. */
export const MAX_BODY_BYTES = 1_000_000;

/** Twice the largest file any studio platform takes (8 MiB); larger is not an image the studio made. */
export const MAX_IMAGE_BYTES = 16 * 1024 * 1024;

/** The only types an image route may answer with, as the API stores a version's. */
export const IMAGE_TYPES: readonly string[] = [
  "image/png",
  "image/jpeg",
  "image/webp",
];

// The disposition the API writes: inline or attachment, named after the version's or the image's UUID.
const CONTENT_DISPOSITION =
  /^(inline|attachment); filename="[0-9a-fA-F-]{36}\.(png|jpg|jpeg|webp)"$/;
// What the API asks of a cache for an image: directives only, no quoted text.
const CACHE_CONTROL = /^[a-z0-9=, -]{1,100}$/i;

const UUID =
  "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";
const VERSION = "[1-9][0-9]{0,8}";
// The form every credential's name has in the API. Nothing else reaches it as one.
const CREDENTIAL_NAME = "[a-z][a-z0-9_]{0,63}";
// The form of a tier's and a role's name in the model configuration, as the API matches it.
const SETTING_KEY = "[a-z][a-z0-9_]{0,63}";

// The whole of a Host header that names this machine, as the API itself matches it.
const LOCAL_HOST = /^(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$/i;

type Method = "GET" | "POST" | "PUT" | "DELETE";

/**
 * What a route takes besides its path: nothing; a list's page (`limit` and `cursor`);
 * for an event stream, where it resumes (`after`, or the `Last-Event-ID` header); or,
 * for an image, nothing either, but its answer is bytes of an allowed type.
 */
type RouteKind = "plain" | "paged" | "stream" | "image";

type AllowedRoute = {
  method: Method;
  /** The API path this route forwards to, kept in step with the contract by its type. */
  template: keyof paths;
  pattern: RegExp;
  kind: RouteKind;
};

function route(
  method: Method,
  template: keyof paths,
  kind: RouteKind = "plain",
): AllowedRoute {
  // An image is only ever read: its bytes are never a request's body.
  if (kind === "image" && method !== "GET")
    throw new Error(`an image route is a GET: ${template}`);
  const pattern = template
    .replace(
      /\{(project_id|run_id|proposal_id|chat_id|asset_id|version_id|image_id)\}/g,
      UUID,
    )
    .replace(/\{version\}/g, VERSION)
    .replace(/\{name\}/g, CREDENTIAL_NAME)
    .replace(/\{(tier|role)\}/g, SETTING_KEY);
  return { method, template, pattern: new RegExp(`^${pattern}$`), kind };
}

const PAGED = "paged";
const STREAM = "stream";
const IMAGE = "image";

/** Every API route the dashboard uses. Nothing else is forwarded. */
export const ALLOWED_ROUTES: readonly AllowedRoute[] = [
  route("GET", "/v1/projects", PAGED),
  route("POST", "/v1/projects"),
  route("GET", "/v1/projects/{project_id}"),
  route("DELETE", "/v1/projects/{project_id}"),
  route("GET", "/v1/projects/{project_id}/analyses", PAGED),
  route("POST", "/v1/projects/{project_id}/analyses"),
  route("GET", "/v1/projects/{project_id}/analyses/{run_id}"),
  route("GET", "/v1/projects/{project_id}/researches", PAGED),
  route("GET", "/v1/projects/{project_id}/researches/{run_id}"),
  route("GET", "/v1/projects/{project_id}/researches/{run_id}/report"),
  route("DELETE", "/v1/projects/{project_id}/researches/{run_id}"),
  route("GET", "/v1/projects/{project_id}/studio-runs", PAGED),
  route("POST", "/v1/projects/{project_id}/studio-runs"),
  route("GET", "/v1/projects/{project_id}/studio-runs/{run_id}"),
  route("DELETE", "/v1/projects/{project_id}/studio-runs/{run_id}"),
  route("GET", "/v1/projects/{project_id}/media-assets", PAGED),
  route("GET", "/v1/projects/{project_id}/media-assets/{asset_id}"),
  route("POST", "/v1/projects/{project_id}/media-assets/{asset_id}/accept"),
  route("POST", "/v1/projects/{project_id}/media-assets/{asset_id}/reject"),
  route(
    "GET",
    "/v1/projects/{project_id}/media-assets/{asset_id}/versions/{version_id}/image",
    IMAGE,
  ),
  route(
    "GET",
    "/v1/projects/{project_id}/media-assets/{asset_id}/versions/{version_id}/download",
    IMAGE,
  ),
  route("GET", "/v1/projects/{project_id}/repository-images", PAGED),
  route(
    "GET",
    "/v1/projects/{project_id}/repository-images/{image_id}/image",
    IMAGE,
  ),
  route("GET", "/v1/projects/{project_id}/chats", PAGED),
  route("POST", "/v1/projects/{project_id}/chats"),
  route("GET", "/v1/projects/{project_id}/chats/{chat_id}"),
  route("GET", "/v1/projects/{project_id}/chats/{chat_id}/messages", PAGED),
  route("POST", "/v1/projects/{project_id}/chats/{chat_id}/messages"),
  route("POST", "/v1/projects/{project_id}/chats/{chat_id}/read"),
  route("DELETE", "/v1/projects/{project_id}/chats/{chat_id}"),
  route("GET", "/v1/projects/{project_id}/chats/{chat_id}/events", PAGED),
  route(
    "GET",
    "/v1/projects/{project_id}/chats/{chat_id}/events/stream",
    STREAM,
  ),
  route("GET", "/v1/projects/{project_id}/profile/versions", PAGED),
  route("GET", "/v1/projects/{project_id}/profile/versions/{version}"),
  route("POST", "/v1/projects/{project_id}/profile/versions/{version}/edits"),
  route(
    "POST",
    "/v1/projects/{project_id}/profile/versions/{version}/approval",
  ),
  route("GET", "/v1/projects/{project_id}/proposals", PAGED),
  route("POST", "/v1/projects/{project_id}/proposals/{proposal_id}/approval"),
  route("POST", "/v1/projects/{project_id}/proposals/{proposal_id}/rejection"),
  route("GET", "/v1/proposals", PAGED),
  route("GET", "/v1/events/stream", STREAM),
  route("GET", "/v1/settings/credentials"),
  route("PUT", "/v1/settings/credentials/{name}"),
  route("DELETE", "/v1/settings/credentials/{name}"),
  route("GET", "/v1/settings/models"),
  route("PUT", "/v1/settings/models/tiers/{tier}"),
  route("DELETE", "/v1/settings/models/tiers/{tier}"),
  route("PUT", "/v1/settings/models/roles/{role}"),
  route("DELETE", "/v1/settings/models/roles/{role}"),
  route("PUT", "/v1/settings/models/rejected-parameters"),
  route("GET", "/v1/settings/models/catalog"),
  route("GET", "/v1/settings/models/check"),
  route("GET", "/v1/settings/analysis"),
  route("PUT", "/v1/settings/analysis"),
  route("DELETE", "/v1/settings/analysis"),
  route("GET", "/v1/settings/creative-budget"),
  route("PUT", "/v1/settings/creative-budget"),
  route("DELETE", "/v1/settings/creative-budget"),
];

// What a page of a list may be asked for, as the contract has it: 1 to 100 items,
// and the API's own opaque cursor (URL-safe base64, at most 200 characters).
const MAX_PAGE_SIZE = 100;
const LIMIT = /^[1-9][0-9]{0,2}$/;
const CURSOR = /^[A-Za-z0-9_-]{1,200}$/;
// An event's id, as the API numbers them: a non-negative integer in decimal.
const EVENT_ID = /^(0|[1-9][0-9]{0,17})$/;

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
 *   (every method but GET) must carry it: browsers always send it with a POST, a
 *   PUT or a DELETE, so its absence means the caller is not the dashboard.
 */
export function assertFromDashboard(method: string, headers: Headers): void {
  const host = headers.get("host");
  if (host === null || !LOCAL_HOST.test(host)) {
    throw refused(
      "the dashboard serves only requests addressed to this machine",
    );
  }

  const changesState = method !== "GET";
  const fetchSite = headers.get("sec-fetch-site");
  const fetchSiteAllowed = changesState
    ? ["same-origin"]
    : ["same-origin", "none"];
  if (fetchSite !== null && !fetchSiteAllowed.includes(fetchSite)) {
    throw refused("the request was made by another site");
  }

  const origin = headers.get("origin");
  if (origin === null) {
    if (changesState)
      throw refused("a request that changes something must carry an Origin");
    return;
  }
  if (origin !== `http://${host}` && origin !== `https://${host}`) {
    throw refused("the request was made by another site");
  }
}

/** Where an allowed request goes: the API path and query, and what kind of route answers it. */
export type ApiTarget = { pathAndQuery: string; kind: RouteKind };

/**
 * The API path and query for an allowed request; refuses every other method and path.
 *
 * A query is refused rather than dropped, so a caller learns that what it sent had
 * no effect: only a paged list takes one, only `limit` and `cursor`, each once and
 * each in the form the API issues; and the event stream takes only `after`, once,
 * as an event id.
 */
export function allowedApiTarget(
  method: string,
  pathSegments: readonly string[],
  query: URLSearchParams,
): string {
  return allowedRoute(method, pathSegments, query).pathAndQuery;
}

function allowedRoute(
  method: string,
  pathSegments: readonly string[],
  query: URLSearchParams,
): ApiTarget {
  const path = `/v1/${pathSegments.join("/")}`;
  const matched = ALLOWED_ROUTES.find(
    (r) => r.method === method && r.pattern.test(path),
  );
  if (matched === undefined) {
    throw new ProxyRefusal(
      404,
      "proxy_route_not_allowed",
      "the dashboard does not forward this method and path",
    );
  }
  const forwarded = routeQuery(query, matched.kind);
  return {
    pathAndQuery: forwarded === "" ? path : `${path}?${forwarded}`,
    kind: matched.kind,
  };
}

const QUERY_RULE: Record<RouteKind, string> = {
  plain: "this route takes no query parameters",
  paged:
    "a list takes only `limit` (1 to 100) and `cursor` (as the API issued it), each once",
  stream: "the event stream takes only `after`, once, as an event id",
  image: "this route takes no query parameters",
};

function routeQuery(query: URLSearchParams, kind: RouteKind): string {
  const forwarded = new URLSearchParams();
  for (const name of new Set(query.keys())) {
    const values = query.getAll(name);
    if (values.length !== 1 || !isAllowedParameter(kind, name, values[0])) {
      throw new ProxyRefusal(400, "proxy_query_rejected", QUERY_RULE[kind]);
    }
    forwarded.set(name, values[0]);
  }
  return forwarded.toString();
}

function isAllowedParameter(
  kind: RouteKind,
  name: string,
  value: string,
): boolean {
  if (kind === "paged") {
    return (
      (name === "limit" &&
        LIMIT.test(value) &&
        Number(value) <= MAX_PAGE_SIZE) ||
      (name === "cursor" && CURSOR.test(value))
    );
  }
  if (kind === "stream") return name === "after" && EVENT_ID.test(value);
  return false;
}

/**
 * The `Last-Event-ID` a browser's `EventSource` sends when it reconnects, checked to be
 * an event id; nothing when the browser sent none. Read for the event stream only.
 */
export function lastEventId(headers: Headers): string | undefined {
  const value = headers.get("last-event-id");
  if (value === null) return undefined;
  if (!EVENT_ID.test(value)) {
    throw new ProxyRefusal(
      400,
      "proxy_header_rejected",
      "`Last-Event-ID` must be an event id",
    );
  }
  return value;
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

/**
 * The JSON body to forward, or none. Anything that is not JSON, or is too large, is
 * refused, and so is a body sent with a DELETE, which the API takes none for.
 */
export async function readJsonBody(
  request: Request,
): Promise<string | undefined> {
  if (request.method === "GET") return undefined;
  const body = await request.text();
  if (body === "") return undefined;
  if (request.method === "DELETE") {
    throw new ProxyRefusal(
      400,
      "proxy_body_rejected",
      "a DELETE takes no request body",
    );
  }
  const mediaType = request.headers
    .get("content-type")
    ?.split(";")[0]
    .trim()
    .toLowerCase();
  if (mediaType !== "application/json") {
    throw new ProxyRefusal(
      415,
      "proxy_body_rejected",
      "a request body must be application/json",
    );
  }
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) {
    throw new ProxyRefusal(
      413,
      "proxy_body_rejected",
      "the request body is too large",
    );
  }
  return body;
}

/**
 * Check a request and forward it to the API, or answer with the refusal.
 *
 * Only the method, the validated path, a list's page parameters and the JSON body
 * travel upstream: no cookie, no other query and no header of the browser's, except
 * the event stream's checked `Last-Event-ID`.
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
    const { pathAndQuery, kind } = allowedRoute(
      request.method,
      pathSegments,
      query,
    );
    const target = new URL(pathAndQuery, apiBaseUrl(configuredApiUrl));
    if (kind === "stream")
      return await forwardStream(
        send,
        target,
        lastEventId(request.headers),
        request.signal,
      );
    if (kind === "image")
      return await forwardImage(send, target, request.signal);
    const body = await readJsonBody(request);
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
  const upstream = await reach(send, target, {
    method,
    body,
    headers: {
      accept: "application/json",
      ...(body === undefined ? {} : { "content-type": "application/json" }),
    },
  });
  const text = await upstream.text();
  // A status that may not carry a body (204, 304) cannot be given one.
  return jsonResponse(upstream.status, text === "" ? null : text);
}

/**
 * Forward an event stream: its events are passed on as the API sends them, never read
 * whole, and the upstream request is aborted when the browser goes away (`signal`). An
 * answer that is not an event stream (the API's 404 for a chat it does not have, or its
 * 503 when it has too many streams open) is passed on as JSON, like any other.
 */
async function forwardStream(
  send: typeof fetch,
  target: URL,
  resumeAfter: string | undefined,
  signal: AbortSignal,
): Promise<Response> {
  const upstream = await reach(send, target, {
    method: "GET",
    headers: {
      accept: "text/event-stream",
      ...(resumeAfter === undefined ? {} : { "last-event-id": resumeAfter }),
    },
    signal,
  });
  const mediaType = upstream.headers
    .get("content-type")
    ?.split(";")[0]
    .trim()
    .toLowerCase();
  if (
    !upstream.ok ||
    mediaType !== "text/event-stream" ||
    upstream.body === null
  ) {
    const text = await upstream.text();
    return jsonResponse(upstream.status, text === "" ? null : text);
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      // `no-transform` keeps compression from holding events back; `X-Accel-Buffering`
      // asks a reverse proxy in front of the dashboard not to buffer them either.
      "cache-control": "no-store, no-transform",
      "x-accel-buffering": "no",
      "x-content-type-options": "nosniff",
    },
  });
}

/**
 * Forward an image: its bytes are passed on only when the API says it is one of
 * `IMAGE_TYPES` and it is no larger than `MAX_IMAGE_BYTES`, and only the headers an image
 * needs come back. An answer that is not a success (the API's 404 for a version it does
 * not have) is passed on as JSON, like any other.
 */
async function forwardImage(
  send: typeof fetch,
  target: URL,
  signal: AbortSignal,
): Promise<Response> {
  const upstream = await reach(send, target, {
    method: "GET",
    headers: { accept: IMAGE_TYPES.join(", ") },
    signal,
  });
  if (!upstream.ok) {
    const text = await upstream.text();
    return jsonResponse(upstream.status, text === "" ? null : text);
  }
  const mediaType = upstream.headers
    .get("content-type")
    ?.split(";")[0]
    .trim()
    .toLowerCase();
  if (mediaType === undefined || !IMAGE_TYPES.includes(mediaType)) {
    await upstream.body?.cancel();
    throw upstreamRejected(
      "the API answered with something that is not a PNG, JPEG or WebP image",
    );
  }
  const declared = Number(upstream.headers.get("content-length") ?? "0");
  if (declared > MAX_IMAGE_BYTES) {
    await upstream.body?.cancel();
    throw upstreamRejected("the image is larger than the dashboard passes on");
  }
  const bytes = await readAtMost(upstream, MAX_IMAGE_BYTES);
  const headers = new Headers({
    "content-type": mediaType,
    "content-length": String(bytes.byteLength),
    "cache-control":
      headerMatching(upstream, "cache-control", CACHE_CONTROL) ?? "no-store",
    "x-content-type-options": "nosniff",
  });
  const disposition = headerMatching(
    upstream,
    "content-disposition",
    CONTENT_DISPOSITION,
  );
  if (disposition !== null) headers.set("content-disposition", disposition);
  return new Response(bytes, { status: upstream.status, headers });
}

/** The whole body, read a chunk at a time and refused as soon as it passes `limit` bytes. */
async function readAtMost(
  upstream: Response,
  limit: number,
): Promise<Uint8Array<ArrayBuffer>> {
  if (upstream.body === null) return new Uint8Array(0);
  const reader = upstream.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.byteLength;
    if (length > limit) {
      await reader.cancel();
      throw upstreamRejected(
        "the image is larger than the dashboard passes on",
      );
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

/** An upstream header in the form the API writes it, or null for anything else or none. */
function headerMatching(
  upstream: Response,
  name: string,
  form: RegExp,
): string | null {
  const value = upstream.headers.get(name);
  return value !== null && form.test(value) ? value : null;
}

function upstreamRejected(message: string): ProxyRefusal {
  return new ProxyRefusal(502, "proxy_upstream_rejected", message);
}

async function reach(
  send: typeof fetch,
  target: URL,
  init: RequestInit,
): Promise<Response> {
  try {
    return await send(target, {
      ...init,
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
