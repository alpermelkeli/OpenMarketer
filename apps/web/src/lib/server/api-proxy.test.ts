import { afterEach, describe, expect, it, vi } from "vitest";

import { MAX_BODY_BYTES, MAX_IMAGE_BYTES, allowedApiTarget, apiBaseUrl, assertFromDashboard, proxyToApi } from "./api-proxy";

const PROJECT = "8ce5e2bb-6e54-49b4-9ddc-b96f20ec1109";
const RUN = "0b0e6c6e-1111-4222-8333-444455556666";
const APPROVAL = ["projects", PROJECT, "profile", "versions", "3", "approval"];

function request(method: string, headers: Record<string, string>, body?: string, query = ""): Request {
  return new Request(`http://localhost:3000/api/v1/ignored${query}`, { method, headers, body });
}

const NO_QUERY = new URLSearchParams();

function allowedApiPath(method: string, segments: readonly string[]): string {
  return allowedApiTarget(method, segments, NO_QUERY);
}

const fromDashboard = {
  host: "localhost:3000",
  origin: "http://localhost:3000",
  "sec-fetch-site": "same-origin",
};

function apiAnswering(status: number, body: string) {
  return vi.fn<typeof fetch>(async () => new Response(body, { status }));
}

describe("a request from another site", () => {
  it("is refused when its Origin is another site", async () => {
    const api = apiAnswering(200, "{}");
    const response = await proxyToApi(
      request("POST", { host: "localhost:3000", origin: "https://evil.example" }),
      APPROVAL,
      undefined,
      api,
    );
    expect(response.status).toBe(403);
    expect(await response.json()).toMatchObject({ code: "proxy_request_refused" });
    expect(api).not.toHaveBeenCalled();
  });

  it("is refused when the browser marks it cross-site, even with a forged-looking Origin", async () => {
    const api = apiAnswering(200, "{}");
    const response = await proxyToApi(
      request("POST", { ...fromDashboard, "sec-fetch-site": "cross-site" }),
      APPROVAL,
      undefined,
      api,
    );
    expect(response.status).toBe(403);
    expect(api).not.toHaveBeenCalled();
  });

  it("is refused when it comes from another port of this machine", () => {
    const headers = new Headers({ host: "localhost:3000", origin: "http://localhost:5173" });
    expect(() => assertFromDashboard("POST", headers)).toThrow("another site");
  });

  it("is refused when a same-site page that is not this origin makes it", () => {
    const headers = new Headers({ ...fromDashboard, "sec-fetch-site": "same-site" });
    expect(() => assertFromDashboard("POST", headers)).toThrow("another site");
  });

  it("is refused when its Origin is opaque", () => {
    const headers = new Headers({ host: "localhost:3000", origin: "null" });
    expect(() => assertFromDashboard("POST", headers)).toThrow("another site");
  });

  it("is refused when the Host is a foreign name pointed at this machine", () => {
    const headers = new Headers({ host: "evil.example:3000", origin: "http://evil.example:3000" });
    expect(() => assertFromDashboard("POST", headers)).toThrow("addressed to this machine");
  });

  it("is refused when the Host only looks local", () => {
    for (const host of ["localhost.evil.example", "localhost@evil.example", "127.0.0.1.nip.io"]) {
      expect(() => assertFromDashboard("GET", new Headers({ host }))).toThrow();
    }
  });

  it("cannot read through the proxy either", () => {
    const headers = new Headers({ host: "localhost:3000", "sec-fetch-site": "cross-site" });
    expect(() => assertFromDashboard("GET", headers)).toThrow("another site");
  });
});

describe("a request that changes something", () => {
  it("is refused without an Origin", () => {
    const headers = new Headers({ host: "localhost:3000" });
    expect(() => assertFromDashboard("POST", headers)).toThrow("must carry an Origin");
  });

  it("is refused as a top-level navigation", () => {
    const headers = new Headers({ ...fromDashboard, "sec-fetch-site": "none" });
    expect(() => assertFromDashboard("POST", headers)).toThrow("another site");
  });

  it("is forwarded from the dashboard's own page, without the browser's headers", async () => {
    const api = apiAnswering(200, '{"status":"approved"}');
    const response = await proxyToApi(
      request("POST", { ...fromDashboard, cookie: "session=1" }),
      APPROVAL,
      undefined,
      api,
    );
    expect(response.status).toBe(200);
    const [target, init] = api.mock.calls[0];
    expect(String(target)).toBe(
      `http://127.0.0.1:8000/v1/projects/${PROJECT}/profile/versions/3/approval`,
    );
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeUndefined();
    expect(init?.headers).toEqual({ accept: "application/json" });
  });

  it("is refused when its body is not JSON", async () => {
    const api = apiAnswering(201, "{}");
    const response = await proxyToApi(
      request("POST", { ...fromDashboard, "content-type": "text/plain" }, '{"name":"x"}'),
      ["projects"],
      undefined,
      api,
    );
    expect(response.status).toBe(415);
    expect(api).not.toHaveBeenCalled();
  });
});

describe("a read from the dashboard", () => {
  it("needs no Origin, as browsers send none with a same-origin GET", () => {
    const headers = new Headers({ host: "127.0.0.1:3000", "sec-fetch-site": "same-origin" });
    expect(() => assertFromDashboard("GET", headers)).not.toThrow();
  });

  it("passes the API's status and body on", async () => {
    const problem = '{"code":"profile_not_found","message":"no draft"}';
    const response = await proxyToApi(
      request("GET", { host: "localhost:3000", "sec-fetch-site": "same-origin" }),
      ["projects", PROJECT, "profile", "versions"],
      "http://127.0.0.1:9999",
      apiAnswering(404, problem),
    );
    expect(response.status).toBe(404);
    expect(await response.text()).toBe(problem);
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("says so when the API does not answer", async () => {
    const down = vi.fn<typeof fetch>(async () => {
      throw new TypeError("fetch failed");
    });
    const response = await proxyToApi(
      request("GET", { host: "localhost:3000" }),
      ["projects", PROJECT, "analyses", RUN],
      undefined,
      down,
    );
    expect(response.status).toBe(502);
    expect(await response.json()).toMatchObject({ code: "api_unreachable" });
  });
});

describe("the routes that are forwarded", () => {
  it("are the ones the dashboard uses", () => {
    expect(allowedApiPath("POST", ["projects"])).toBe("/v1/projects");
    expect(allowedApiPath("GET", ["projects", PROJECT, "analyses", RUN])).toBe(
      `/v1/projects/${PROJECT}/analyses/${RUN}`,
    );
    expect(allowedApiPath("POST", ["projects", PROJECT, "profile", "versions", "12", "edits"])).toBe(
      `/v1/projects/${PROJECT}/profile/versions/12/edits`,
    );
  });

  it("do not include other methods of a known path", () => {
    expect(() => allowedApiPath("PUT", ["projects"])).toThrow();
    expect(() => allowedApiPath("DELETE", ["projects"])).toThrow();
    expect(() => allowedApiPath("PUT", ["projects", PROJECT])).toThrow();
    expect(() => allowedApiPath("GET", APPROVAL)).toThrow();
  });

  it("do not include paths outside the list", () => {
    for (const segments of [
      ["health"],
      ["projects", "not-a-uuid", "profile", "versions"],
      ["projects", PROJECT, "profile", "draft"],
      ["projects", PROJECT, "profile", "approved"],
      ["projects", PROJECT, "profile", "versions", "0", "approval"],
      ["projects", PROJECT, "profile", "versions", "1e3", "approval"],
      ["projects", PROJECT, "profile", "versions", ".."],
      ["projects", `${PROJECT}/profile/versions?x=`, "profile", "versions"],
      ["..", "..", "admin"],
      ["projects", `${PROJECT}\n`, "profile", "versions"],
    ]) {
      expect(() => allowedApiPath("GET", segments)).toThrow();
    }
  });
});

describe("the routes of proposals", () => {
  const proposal = (decision: string) => ["projects", PROJECT, "proposals", RUN, decision];

  it("forward the list, an approval, a rejection and the workspace's waiting list", () => {
    expect(allowedApiPath("GET", ["projects", PROJECT, "proposals"])).toBe(`/v1/projects/${PROJECT}/proposals`);
    expect(allowedApiPath("POST", proposal("approval"))).toBe(`/v1/projects/${PROJECT}/proposals/${RUN}/approval`);
    expect(allowedApiPath("POST", proposal("rejection"))).toBe(`/v1/projects/${PROJECT}/proposals/${RUN}/rejection`);
    expect(allowedApiPath("GET", ["proposals"])).toBe("/v1/proposals");
  });

  it("do not include one by its id, a decision by GET, a proposal that is not an id, or a filter", () => {
    expect(() => allowedApiPath("GET", ["projects", PROJECT, "proposals", RUN])).toThrow();
    expect(() => allowedApiPath("GET", proposal("approval"))).toThrow();
    expect(() => allowedApiPath("POST", ["projects", PROJECT, "proposals", "latest", "approval"])).toThrow();
    expect(() => allowedApiPath("POST", ["projects", PROJECT, "proposals", RUN])).toThrow();
    expect(() => allowedApiPath("GET", ["proposals", "latest"])).toThrow();
    for (const segments of [
      ["projects", PROJECT, "proposals"],
      ["proposals"],
    ]) {
      expect(() => allowedApiTarget("GET", segments, new URLSearchParams("status=pending"))).toThrow();
    }
  });
});

describe("the routes of a project's chat", () => {
  const CHAT = "0b0e6c6e-aaaa-4bbb-8ccc-ddddeeeeffff";
  const CHATS = ["projects", PROJECT, "chats"];

  it("forward the chats, one of them, its messages, its read mark and its deletion", () => {
    expect(allowedApiPath("GET", CHATS)).toBe(`/v1/projects/${PROJECT}/chats`);
    expect(allowedApiPath("POST", CHATS)).toBe(`/v1/projects/${PROJECT}/chats`);
    expect(allowedApiPath("GET", [...CHATS, CHAT])).toBe(`/v1/projects/${PROJECT}/chats/${CHAT}`);
    expect(allowedApiPath("GET", [...CHATS, CHAT, "messages"])).toBe(
      `/v1/projects/${PROJECT}/chats/${CHAT}/messages`,
    );
    expect(allowedApiPath("POST", [...CHATS, CHAT, "messages"])).toBe(
      `/v1/projects/${PROJECT}/chats/${CHAT}/messages`,
    );
    expect(allowedApiPath("POST", [...CHATS, CHAT, "read"])).toBe(
      `/v1/projects/${PROJECT}/chats/${CHAT}/read`,
    );
    expect(allowedApiPath("DELETE", [...CHATS, CHAT])).toBe(
      `/v1/projects/${PROJECT}/chats/${CHAT}`,
    );
  });

  it("do not include a wrong method, a chat that is not an id, or a path below one", () => {
    expect(() => allowedApiPath("GET", [...CHATS, CHAT, "read"])).toThrow();
    expect(() => allowedApiPath("POST", [...CHATS, CHAT])).toThrow();
    expect(() => allowedApiPath("POST", [...CHATS, CHAT, "messages", RUN])).toThrow();
    expect(() => allowedApiPath("GET", [...CHATS, "latest"])).toThrow();
    expect(() => allowedApiPath("DELETE", CHATS)).toThrow();
    expect(() => allowedApiPath("DELETE", [...CHATS, "latest"])).toThrow();
    expect(() => allowedApiPath("GET", ["projects", PROJECT, "chat"])).toThrow();
    expect(() =>
      allowedApiTarget("GET", [...CHATS, CHAT, "messages"], new URLSearchParams("role=user")),
    ).toThrow();
  });
});

describe("the routes of research", () => {
  const RESEARCHES = ["projects", PROJECT, "researches"];
  const RESEARCH = "0b0e6c6e-2222-4333-8444-555566667777";

  it("forward the runs, the report of one and the deletion of one", () => {
    expect(allowedApiPath("GET", RESEARCHES)).toBe(`/v1/projects/${PROJECT}/researches`);
    expect(allowedApiPath("GET", [...RESEARCHES, RESEARCH])).toBe(
      `/v1/projects/${PROJECT}/researches/${RESEARCH}`,
    );
    expect(allowedApiPath("GET", [...RESEARCHES, RESEARCH, "report"])).toBe(
      `/v1/projects/${PROJECT}/researches/${RESEARCH}/report`,
    );
    expect(allowedApiPath("DELETE", [...RESEARCHES, RESEARCH])).toBe(
      `/v1/projects/${PROJECT}/researches/${RESEARCH}`,
    );
  });

  it("do not include a wrong method, a run that is not an id, a path below one, or a filter", () => {
    expect(() => allowedApiPath("POST", RESEARCHES)).toThrow();
    expect(() => allowedApiPath("DELETE", RESEARCHES)).toThrow();
    expect(() => allowedApiPath("DELETE", [...RESEARCHES, "latest"])).toThrow();
    expect(() => allowedApiPath("DELETE", [...RESEARCHES, RESEARCH, "report"])).toThrow();
    expect(() => allowedApiPath("GET", [...RESEARCHES, "latest"])).toThrow();
    expect(() => allowedApiPath("GET", [...RESEARCHES, RESEARCH, "pages"])).toThrow();
    expect(() =>
      allowedApiTarget("GET", RESEARCHES, new URLSearchParams("status=succeeded")),
    ).toThrow();
  });
});

describe("the routes of the studio", () => {
  const RUNS = ["projects", PROJECT, "studio-runs"];
  const ASSETS = ["projects", PROJECT, "media-assets"];
  const ASSET = "0b0e6c6e-3333-4444-8555-666677778888";
  const VERSION = "0b0e6c6e-9999-4aaa-8bbb-ccccddddeeee";

  it("forward the runs, starting and deleting one, and the library with its decisions", () => {
    expect(allowedApiPath("GET", RUNS)).toBe(`/v1/projects/${PROJECT}/studio-runs`);
    expect(allowedApiPath("POST", RUNS)).toBe(`/v1/projects/${PROJECT}/studio-runs`);
    expect(allowedApiPath("GET", [...RUNS, RUN])).toBe(`/v1/projects/${PROJECT}/studio-runs/${RUN}`);
    expect(allowedApiPath("DELETE", [...RUNS, RUN])).toBe(`/v1/projects/${PROJECT}/studio-runs/${RUN}`);
    expect(allowedApiPath("GET", ASSETS)).toBe(`/v1/projects/${PROJECT}/media-assets`);
    expect(allowedApiPath("GET", [...ASSETS, ASSET])).toBe(`/v1/projects/${PROJECT}/media-assets/${ASSET}`);
    expect(allowedApiPath("POST", [...ASSETS, ASSET, "accept"])).toBe(
      `/v1/projects/${PROJECT}/media-assets/${ASSET}/accept`,
    );
    expect(allowedApiPath("POST", [...ASSETS, ASSET, "reject"])).toBe(
      `/v1/projects/${PROJECT}/media-assets/${ASSET}/reject`,
    );
  });

  it("page the runs and the library", () => {
    expect(allowedApiTarget("GET", RUNS, new URLSearchParams("limit=20"))).toBe(
      `/v1/projects/${PROJECT}/studio-runs?limit=20`,
    );
    expect(allowedApiTarget("GET", ASSETS, new URLSearchParams("cursor=abc"))).toBe(
      `/v1/projects/${PROJECT}/media-assets?cursor=abc`,
    );
  });

  it("forward a version's image and its download as reads only, with no query", () => {
    const image = [...ASSETS, ASSET, "versions", VERSION, "image"];
    const download = [...ASSETS, ASSET, "versions", VERSION, "download"];
    expect(allowedApiPath("GET", image)).toBe(`/v1/projects/${PROJECT}/media-assets/${ASSET}/versions/${VERSION}/image`);
    expect(allowedApiPath("GET", download)).toBe(
      `/v1/projects/${PROJECT}/media-assets/${ASSET}/versions/${VERSION}/download`,
    );
    for (const method of ["POST", "PUT", "DELETE"]) {
      expect(() => allowedApiPath(method, image)).toThrow();
      expect(() => allowedApiPath(method, download)).toThrow();
    }
    expect(() => allowedApiTarget("GET", image, new URLSearchParams("size=small"))).toThrow();
  });

  it("do not include a wrong method, an id of another form, or a path below one", () => {
    expect(() => allowedApiPath("PUT", RUNS)).toThrow();
    expect(() => allowedApiPath("POST", [...RUNS, RUN])).toThrow();
    expect(() => allowedApiPath("GET", [...RUNS, "latest"])).toThrow();
    expect(() => allowedApiPath("DELETE", [...ASSETS, ASSET])).toThrow();
    expect(() => allowedApiPath("GET", [...ASSETS, ASSET, "accept"])).toThrow();
    expect(() => allowedApiPath("GET", [...ASSETS, ASSET, "versions"])).toThrow();
    expect(() => allowedApiPath("GET", [...ASSETS, ASSET, "versions", "1", "image"])).toThrow();
    expect(() => allowedApiPath("GET", [...ASSETS, ASSET, "versions", VERSION, "code"])).toThrow();
  });

  it("forward reading, choosing and removing the creative budget", () => {
    for (const method of ["GET", "PUT", "DELETE"]) {
      expect(allowedApiPath(method, ["settings", "creative-budget"])).toBe("/v1/settings/creative-budget");
    }
    expect(() => allowedApiPath("POST", ["settings", "creative-budget"])).toThrow();
  });
});

describe("an image the API answers with", () => {
  const ASSET = "0b0e6c6e-3333-4444-8555-666677778888";
  const VERSION = "0b0e6c6e-9999-4aaa-8bbb-ccccddddeeee";
  const IMAGE = ["projects", PROJECT, "media-assets", ASSET, "versions", VERSION, "image"];
  const reading = { host: "localhost:3000", "sec-fetch-site": "same-origin" };
  const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

  function apiImage(body: BodyInit | null, headers: Record<string, string>, status = 200) {
    return vi.fn<typeof fetch>(async () => new Response(body, { status, headers }));
  }

  async function read(api: typeof fetch, headers: Record<string, string> = reading) {
    return proxyToApi(request("GET", headers), IMAGE, undefined, api);
  }

  it("is passed on as bytes, asked for as an image", async () => {
    const api = apiImage(PNG, { "content-type": "image/png" });
    const response = await read(api);
    expect(response.status).toBe(200);
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(PNG);
    expect(response.headers.get("content-type")).toBe("image/png");
    expect(response.headers.get("content-length")).toBe(String(PNG.byteLength));
    expect(api.mock.calls[0][1]?.headers).toEqual({ accept: "image/png, image/jpeg, image/webp" });
  });

  it("is passed on for each of the three types the studio stores", async () => {
    for (const type of ["image/png", "image/jpeg", "image/webp"]) {
      const response = await read(apiImage(PNG, { "content-type": type }));
      expect(response.status).toBe(200);
      expect(response.headers.get("content-type")).toBe(type);
    }
  });

  it("is refused when it is any other type", async () => {
    for (const type of ["text/html", "image/svg+xml", "application/json", "application/octet-stream"]) {
      const response = await read(apiImage("<svg onload=alert(1)>", { "content-type": type }));
      expect(response.status).toBe(502);
      expect(response.headers.get("content-type")).toBe("application/json");
      expect(await response.json()).toMatchObject({ code: "proxy_upstream_rejected" });
    }
    const untyped = await read(apiImage(new Blob([PNG]), {}));
    expect(untyped.status).toBe(502);
  });

  it("is refused when it is larger than the limit, declared or not", async () => {
    const declared = await read(
      apiImage(PNG, { "content-type": "image/png", "content-length": String(MAX_IMAGE_BYTES + 1) }),
    );
    expect(declared.status).toBe(502);
    const large = new Uint8Array(MAX_IMAGE_BYTES + 1);
    const undeclared = await read(
      apiImage(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(large);
            controller.close();
          },
        }),
        { "content-type": "image/png" },
      ),
    );
    expect(undeclared.status).toBe(502);
    expect(await undeclared.json()).toMatchObject({ code: "proxy_upstream_rejected" });
  });

  it("comes back with the five headers an image needs and no other", async () => {
    const response = await read(
      apiImage(PNG, {
        "content-type": "image/png; charset=binary",
        "content-disposition": `attachment; filename="${VERSION}.png"`,
        "cache-control": "private, max-age=86400, immutable",
        "set-cookie": "session=1",
        "x-internal": "secret",
        link: "<https://evil.example>; rel=preload",
      }),
    );
    expect([...response.headers.keys()].sort()).toEqual([
      "cache-control",
      "content-disposition",
      "content-length",
      "content-type",
      "x-content-type-options",
    ]);
    expect(response.headers.get("content-type")).toBe("image/png");
    expect(response.headers.get("content-disposition")).toBe(`attachment; filename="${VERSION}.png"`);
    expect(response.headers.get("cache-control")).toBe("private, max-age=86400, immutable");
    expect(response.headers.get("x-content-type-options")).toBe("nosniff");
  });

  it("drops a disposition or a cache rule the API does not write", async () => {
    const response = await read(
      apiImage(PNG, {
        "content-type": "image/png",
        "content-disposition": 'attachment; filename="../../evil.html"',
        "cache-control": 'public, no-cache="set-cookie"',
      }),
    );
    expect(response.headers.get("content-disposition")).toBeNull();
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("passes the API's refusal on as JSON", async () => {
    const problem = '{"code":"media_asset_version_not_found","message":"no such version"}';
    const response = await read(apiImage(problem, { "content-type": "application/json" }, 404));
    expect(response.status).toBe(404);
    expect(response.headers.get("content-type")).toBe("application/json");
    expect(await response.text()).toBe(problem);
  });

  it("is not read for another site", async () => {
    const api = apiImage(PNG, { "content-type": "image/png" });
    const response = await read(api, { host: "localhost:3000", "sec-fetch-site": "cross-site" });
    expect(response.status).toBe(403);
    expect(api).not.toHaveBeenCalled();
  });
});

describe("the events of a chat", () => {
  const CHAT = "0b0e6c6e-aaaa-4bbb-8ccc-ddddeeeeffff";
  const EVENTS = ["projects", PROJECT, "chats", CHAT, "events"];
  const STREAM = [...EVENTS, "stream"];
  const streamPath = `/v1/projects/${PROJECT}/chats/${CHAT}/events/stream`;
  const reading = { host: "localhost:3000", "sec-fetch-site": "same-origin" };

  function apiStreaming(chunks: readonly string[]) {
    return vi.fn<typeof fetch>(async () => {
      const encoder = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
          controller.close();
        },
      });
      return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
    });
  }

  it("forward the history page by page, and the stream from an event id", () => {
    expect(allowedApiTarget("GET", EVENTS, new URLSearchParams("limit=100&cursor=abc"))).toBe(
      `/v1/projects/${PROJECT}/chats/${CHAT}/events?limit=100&cursor=abc`,
    );
    expect(allowedApiPath("GET", STREAM)).toBe(streamPath);
    expect(allowedApiTarget("GET", STREAM, new URLSearchParams("after=42"))).toBe(`${streamPath}?after=42`);
  });

  it("take no other method, and nothing below the stream", () => {
    expect(() => allowedApiPath("POST", EVENTS)).toThrow();
    expect(() => allowedApiPath("POST", STREAM)).toThrow();
    expect(() => allowedApiPath("GET", [...STREAM, "1"])).toThrow();
    expect(() => allowedApiPath("GET", ["projects", PROJECT, "chats", "latest", "events", "stream"])).toThrow();
  });

  it("refuse an `after` that is not an event id, and every other query on the stream", () => {
    for (const query of ["after=-1", "after=1e3", "after=abc", "after=", "after=01", "after=1&after=2", "limit=5", "cursor=abc"]) {
      expect(() => allowedApiTarget("GET", STREAM, new URLSearchParams(query))).toThrow("only `after`");
    }
    expect(() => allowedApiTarget("GET", EVENTS, new URLSearchParams("after=1"))).toThrow("only `limit`");
  });

  it("pass the stream through as it arrives, with no buffering", async () => {
    const api = apiStreaming(["retry: 1000\n\n", 'id: 3\nevent: tool_called\ndata: {"id":3}\n\n']);
    const response = await proxyToApi(request("GET", reading, undefined, "?after=2"), STREAM, undefined, api);
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/event-stream; charset=utf-8");
    expect(response.headers.get("cache-control")).toBe("no-store, no-transform");
    expect(response.headers.get("x-accel-buffering")).toBe("no");
    expect(await response.text()).toBe('retry: 1000\n\nid: 3\nevent: tool_called\ndata: {"id":3}\n\n');
    const [target, init] = api.mock.calls[0];
    expect(String(target)).toBe(`http://127.0.0.1:8000${streamPath}?after=2`);
    expect(init?.headers).toEqual({ accept: "text/event-stream" });
  });

  it("hand the stream its first chunk before the API has finished sending", async () => {
    let finish: () => void = () => undefined;
    const api = vi.fn<typeof fetch>(async () => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode("retry: 1000\n\n"));
          finish = () => controller.close();
        },
      });
      return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
    });
    const response = await proxyToApi(request("GET", reading), STREAM, undefined, api);
    const reader = response.body?.getReader();
    const first = await reader?.read();
    expect(new TextDecoder().decode(first?.value)).toBe("retry: 1000\n\n");
    finish();
  });

  it("forward the browser's Last-Event-ID, and only when it is an event id", async () => {
    const api = apiStreaming([]);
    await proxyToApi(request("GET", { ...reading, "last-event-id": "17" }), STREAM, undefined, api);
    expect(api.mock.calls[0][1]?.headers).toEqual({ accept: "text/event-stream", "last-event-id": "17" });

    const refusedApi = apiStreaming([]);
    const response = await proxyToApi(
      request("GET", { ...reading, "last-event-id": "17, 18" }),
      STREAM,
      undefined,
      refusedApi,
    );
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ code: "proxy_header_rejected" });
    expect(refusedApi).not.toHaveBeenCalled();
  });

  it("do not forward a Last-Event-ID to any other route", async () => {
    const api = apiAnswering(200, '{"events":[],"next_cursor":null}');
    await proxyToApi(request("GET", { ...reading, "last-event-id": "17" }), EVENTS, undefined, api);
    expect(api.mock.calls[0][1]?.headers).toEqual({ accept: "application/json" });
  });

  it("end the upstream request when the browser leaves", async () => {
    const api = apiStreaming([]);
    const browser = new AbortController();
    await proxyToApi(
      new Request("http://localhost:3000/api/v1/ignored", { headers: reading, signal: browser.signal }),
      STREAM,
      undefined,
      api,
    );
    const signal = api.mock.calls[0][1]?.signal;
    expect(signal?.aborted).toBe(false);
    browser.abort();
    expect(signal?.aborted).toBe(true);
  });

  it("pass the API's refusal on as JSON", async () => {
    const problem = '{"code":"chat_not_found","message":"no chat"}';
    const response = await proxyToApi(request("GET", reading), STREAM, undefined, apiAnswering(404, problem));
    expect(response.status).toBe(404);
    expect(response.headers.get("content-type")).toBe("application/json");
    expect(await response.text()).toBe(problem);
  });
});

describe("the events of the workspace", () => {
  const STREAM = ["events", "stream"];
  const streamPath = "/v1/events/stream";
  const reading = { host: "localhost:3000", "sec-fetch-site": "same-origin" };

  function apiStreaming(chunks: readonly string[]) {
    return vi.fn<typeof fetch>(async () => {
      const encoder = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
          controller.close();
        },
      });
      return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
    });
  }

  it("forward the stream from an event id, and nothing else", () => {
    expect(allowedApiPath("GET", STREAM)).toBe(streamPath);
    expect(allowedApiTarget("GET", STREAM, new URLSearchParams("after=42"))).toBe(`${streamPath}?after=42`);
    expect(() => allowedApiPath("POST", STREAM)).toThrow();
    expect(() => allowedApiPath("GET", [...STREAM, "1"])).toThrow();
    expect(() => allowedApiPath("GET", ["events"])).toThrow();
  });

  it("refuse an `after` that is not an event id, and every other query", () => {
    for (const query of ["after=-1", "after=abc", "after=1&after=2", "limit=5", "cursor=abc"]) {
      expect(() => allowedApiTarget("GET", STREAM, new URLSearchParams(query))).toThrow("only `after`");
    }
  });

  it("pass the stream through, with the browser's Last-Event-ID", async () => {
    const api = apiStreaming(['id: 9\nevent: proposal_waiting\ndata: {"id":9}\n\n']);
    const response = await proxyToApi(
      request("GET", { ...reading, "last-event-id": "8" }),
      STREAM,
      undefined,
      api,
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/event-stream; charset=utf-8");
    expect(await response.text()).toBe('id: 9\nevent: proposal_waiting\ndata: {"id":9}\n\n');
    const [target, init] = api.mock.calls[0];
    expect(String(target)).toBe(`http://127.0.0.1:8000${streamPath}`);
    expect(init?.headers).toEqual({ accept: "text/event-stream", "last-event-id": "8" });
  });
});

describe("the route that deletes a project", () => {
  it("takes one project by its id", () => {
    expect(allowedApiPath("DELETE", ["projects", PROJECT])).toBe(`/v1/projects/${PROJECT}`);
  });

  it("deletes nothing under a project and no project named otherwise", () => {
    expect(() => allowedApiPath("DELETE", ["projects", PROJECT, "profile", "versions", "1"])).toThrow();
    expect(() => allowedApiPath("DELETE", ["projects", "not-a-uuid"])).toThrow();
    expect(() => allowedApiPath("DELETE", ["projects", PROJECT, ".."])).toThrow();
  });
});

describe("the routes that read", () => {
  it("are the lists, a project, a run and a version", () => {
    expect(allowedApiPath("GET", ["projects"])).toBe("/v1/projects");
    expect(allowedApiPath("GET", ["projects", PROJECT])).toBe(`/v1/projects/${PROJECT}`);
    expect(allowedApiPath("GET", ["projects", PROJECT, "analyses"])).toBe(`/v1/projects/${PROJECT}/analyses`);
    expect(allowedApiPath("GET", ["projects", PROJECT, "profile", "versions"])).toBe(
      `/v1/projects/${PROJECT}/profile/versions`,
    );
    expect(allowedApiPath("GET", ["projects", PROJECT, "profile", "versions", "7"])).toBe(
      `/v1/projects/${PROJECT}/profile/versions/7`,
    );
  });

  it("cannot be used to change anything", () => {
    expect(() => allowedApiPath("POST", ["projects", PROJECT])).toThrow();
    expect(() => allowedApiPath("POST", ["projects", PROJECT, "profile", "versions"])).toThrow();
    expect(() => allowedApiPath("POST", ["projects", PROJECT, "profile", "versions", "7"])).toThrow();
    expect(() => allowedApiPath("DELETE", ["projects", PROJECT, "analyses"])).toThrow();
  });
});

describe("the query of a list", () => {
  const list = (query: string, segments = ["projects"]) =>
    allowedApiTarget("GET", segments, new URLSearchParams(query));

  it("forwards limit and cursor", () => {
    expect(list("limit=20")).toBe("/v1/projects?limit=20");
    expect(list("cursor=MjAyNi0xMC0wOV9h-b")).toBe("/v1/projects?cursor=MjAyNi0xMC0wOV9h-b");
    expect(list("limit=100&cursor=abc", ["projects", PROJECT, "analyses"])).toBe(
      `/v1/projects/${PROJECT}/analyses?limit=100&cursor=abc`,
    );
    expect(list("cursor=abc", ["projects", PROJECT, "profile", "versions"])).toBe(
      `/v1/projects/${PROJECT}/profile/versions?cursor=abc`,
    );
    expect(list("limit=50", ["projects", PROJECT, "chats"])).toBe(
      `/v1/projects/${PROJECT}/chats?limit=50`,
    );
  });

  it("refuses any other parameter instead of dropping it", () => {
    for (const query of ["workspace=other", "limit=20&debug=1", "Limit=20", "cursor[]=a"]) {
      expect(() => list(query)).toThrow("only `limit`");
    }
  });

  it("refuses a limit outside what the API allows", () => {
    for (const limit of ["0", "101", "1000", "-1", "1e2", "10.0", " 10", "", "0x10"]) {
      expect(() => list(`limit=${encodeURIComponent(limit)}`)).toThrow();
    }
  });

  it("refuses a cursor the API could not have issued", () => {
    const tooLong = "a".repeat(201);
    for (const cursor of ["", tooLong, "a b", "a/b", "a+b", "a=", "a&b", "../x", "a%00"]) {
      expect(() => list(`cursor=${encodeURIComponent(cursor)}`)).toThrow();
    }
  });

  it("refuses a parameter given twice", () => {
    expect(() => list("limit=5&limit=50")).toThrow();
    expect(() => list("cursor=a&cursor=b")).toThrow();
  });

  it("refuses every query on a route that is not a paged list", () => {
    expect(() => list("limit=5", ["projects", PROJECT])).toThrow("no query parameters");
    expect(() => list("cursor=a", ["projects", PROJECT, "profile", "versions", "2"])).toThrow();
    expect(() => allowedApiTarget("POST", ["projects"], new URLSearchParams("limit=5"))).toThrow();
    expect(() => allowedApiTarget("POST", APPROVAL, new URLSearchParams("approved_by=x"))).toThrow();
  });

  it("reaches the API as the only query", async () => {
    const api = apiAnswering(200, '{"projects":[],"next_cursor":null}');
    await proxyToApi(
      request("GET", { host: "localhost:3000", "sec-fetch-site": "same-origin" }, undefined, "?cursor=abc&limit=2"),
      ["projects"],
      undefined,
      api,
    );
    expect(String(api.mock.calls[0][0])).toBe("http://127.0.0.1:8000/v1/projects?cursor=abc&limit=2");
  });

  it("is answered 400 without asking the API when it is refused", async () => {
    const api = apiAnswering(200, "{}");
    const response = await proxyToApi(
      request("GET", { host: "localhost:3000" }, undefined, "?limit=5&workspace=other"),
      ["projects"],
      undefined,
      api,
    );
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ code: "proxy_query_rejected" });
    expect(api).not.toHaveBeenCalled();
  });
});

describe("the routes of the model and analysis settings", () => {
  const MODELS = ["settings", "models"];

  it("forward the settings, a tier, a role, the rejected parameters, the catalogue and the check", () => {
    expect(allowedApiPath("GET", MODELS)).toBe("/v1/settings/models");
    expect(allowedApiPath("PUT", [...MODELS, "tiers", "fast"])).toBe("/v1/settings/models/tiers/fast");
    expect(allowedApiPath("DELETE", [...MODELS, "tiers", "fast"])).toBe("/v1/settings/models/tiers/fast");
    expect(allowedApiPath("PUT", [...MODELS, "roles", "repo_analyzer"])).toBe(
      "/v1/settings/models/roles/repo_analyzer",
    );
    expect(allowedApiPath("DELETE", [...MODELS, "roles", "repo_analyzer"])).toBe(
      "/v1/settings/models/roles/repo_analyzer",
    );
    expect(allowedApiPath("PUT", [...MODELS, "rejected-parameters"])).toBe("/v1/settings/models/rejected-parameters");
    expect(allowedApiPath("GET", [...MODELS, "catalog"])).toBe("/v1/settings/models/catalog");
    expect(allowedApiPath("GET", [...MODELS, "check"])).toBe("/v1/settings/models/check");
  });

  it("forward reading, choosing and removing the limits of an analysis", () => {
    for (const method of ["GET", "PUT", "DELETE"]) {
      expect(allowedApiPath(method, ["settings", "analysis"])).toBe("/v1/settings/analysis");
    }
  });

  it("do not include a tier or a role of another form, or a path below one", () => {
    for (const name of ["", "Fast", "repo-analyzer", "..", "fast%2F..", `a${"b".repeat(64)}`]) {
      expect(() => allowedApiPath("PUT", [...MODELS, "tiers", name])).toThrow();
      expect(() => allowedApiPath("PUT", [...MODELS, "roles", name])).toThrow();
    }
    expect(() => allowedApiPath("PUT", [...MODELS, "roles", "writer", "model"])).toThrow();
    expect(() => allowedApiPath("GET", [...MODELS, "tiers", "fast"])).toThrow();
    expect(() => allowedApiPath("POST", ["settings", "analysis"])).toThrow();
    expect(() => allowedApiPath("DELETE", [...MODELS])).toThrow();
  });
});

describe("the credential routes", () => {
  const LIST = ["settings", "credentials"];
  const ONE = ["settings", "credentials", "github_token"];
  // Not a real credential: a marker that must never turn up where it does not belong.
  const SECRET = "ghp_a-value-that-must-not-be-repeated";
  const json = { ...fromDashboard, "content-type": "application/json" };
  const valueBody = JSON.stringify({ value: SECRET });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("forward the list, the storing of one and the removal of one", () => {
    expect(allowedApiPath("GET", LIST)).toBe("/v1/settings/credentials");
    expect(allowedApiPath("PUT", ONE)).toBe("/v1/settings/credentials/github_token");
    expect(allowedApiPath("DELETE", ONE)).toBe("/v1/settings/credentials/github_token");
    expect(allowedApiPath("PUT", [...LIST, `a${"b".repeat(63)}`])).toContain("/v1/settings/credentials/ab");
  });

  it("do not include any other method on these paths", () => {
    for (const method of ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]) {
      expect(() => allowedApiPath(method, LIST)).toThrow();
    }
    for (const method of ["GET", "POST", "PATCH", "HEAD", "OPTIONS", "put", "delete"]) {
      expect(() => allowedApiPath(method, ONE)).toThrow();
    }
  });

  it("do not include a name of another form", () => {
    for (const name of [
      "",
      "GITHUB_TOKEN",
      "Github_token",
      "1password",
      "_token",
      "github-token",
      "github.token",
      "github token",
      "github_token\n",
      "github_token%2F..",
      "..",
      `a${"b".repeat(64)}`,
      SECRET,
    ]) {
      expect(() => allowedApiPath("PUT", [...LIST, name])).toThrow();
      expect(() => allowedApiPath("DELETE", [...LIST, name])).toThrow();
    }
  });

  it("do not include a path below or beside a credential", () => {
    expect(() => allowedApiPath("PUT", [...ONE, "value"])).toThrow();
    expect(() => allowedApiPath("DELETE", ["settings", "credentials", "github_token", ".."])).toThrow();
    expect(() => allowedApiPath("GET", ["settings"])).toThrow();
    expect(() => allowedApiPath("PUT", ["settings", "github_token"])).toThrow();
  });

  it("take no query", () => {
    expect(() => allowedApiTarget("GET", LIST, new URLSearchParams("limit=5"))).toThrow("no query parameters");
    expect(() => allowedApiTarget("PUT", ONE, new URLSearchParams(`value=${SECRET}`))).toThrow();
    expect(() => allowedApiTarget("DELETE", ONE, new URLSearchParams("force=1"))).toThrow();
  });

  it("send a new value to the API as the body, and nothing of the browser's with it", async () => {
    const api = apiAnswering(200, '{"name":"github_token","source":"stored"}');
    const response = await proxyToApi(
      request("PUT", { ...json, cookie: "session=1", authorization: "Bearer x" }, valueBody),
      ONE,
      undefined,
      api,
    );
    expect(response.status).toBe(200);
    const [target, init] = api.mock.calls[0];
    expect(String(target)).toBe("http://127.0.0.1:8000/v1/settings/credentials/github_token");
    expect(init?.method).toBe("PUT");
    expect(init?.body).toBe(valueBody);
    expect(init?.headers).toEqual({ accept: "application/json", "content-type": "application/json" });
  });

  it("remove without a body, and pass the API's 204 on as a 204 with no body", async () => {
    const api = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
    const response = await proxyToApi(request("DELETE", fromDashboard), ONE, undefined, api);
    expect(response.status).toBe(204);
    expect(response.body).toBeNull();
    expect(await response.text()).toBe("");
    const [target, init] = api.mock.calls[0];
    expect(String(target)).toBe("http://127.0.0.1:8000/v1/settings/credentials/github_token");
    expect(init?.method).toBe("DELETE");
    expect(init?.body).toBeUndefined();
    expect(init?.headers).toEqual({ accept: "application/json" });
  });

  it("refuse a removal that carries a body", async () => {
    const api = apiAnswering(204, "");
    const response = await proxyToApi(request("DELETE", json, valueBody), ONE, undefined, api);
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ code: "proxy_body_rejected" });
    expect(api).not.toHaveBeenCalled();
  });

  it("are refused without an Origin, and nothing is forwarded", async () => {
    for (const [method, body] of [
      ["PUT", valueBody],
      ["DELETE", undefined],
    ] as const) {
      const api = apiAnswering(200, "{}");
      const headers = { host: "localhost:3000", "content-type": "application/json" };
      const response = await proxyToApi(request(method, headers, body), ONE, undefined, api);
      expect(response.status).toBe(403);
      expect(await response.json()).toMatchObject({ code: "proxy_request_refused" });
      expect(api).not.toHaveBeenCalled();
    }
  });

  it("are refused from another site, and nothing is forwarded", async () => {
    const foreign = [
      { host: "localhost:3000", origin: "https://evil.example" },
      { host: "localhost:3000", origin: "http://localhost:5173" },
      { host: "localhost:3000", origin: "null" },
      { ...fromDashboard, "sec-fetch-site": "cross-site" },
      { ...fromDashboard, "sec-fetch-site": "same-site" },
      { ...fromDashboard, "sec-fetch-site": "none" },
      { host: "evil.example:3000", origin: "http://evil.example:3000" },
    ];
    for (const headers of foreign) {
      for (const [method, body] of [
        ["PUT", valueBody],
        ["DELETE", undefined],
      ] as const) {
        const api = apiAnswering(200, "{}");
        const response = await proxyToApi(
          request(method, { ...headers, "content-type": "application/json" }, body),
          ONE,
          undefined,
          api,
        );
        expect(response.status).toBe(403);
        expect(api).not.toHaveBeenCalled();
      }
    }
  });

  it("bound the size of a new value's body", async () => {
    const api = apiAnswering(200, "{}");
    const tooLarge = JSON.stringify({ value: "a".repeat(MAX_BODY_BYTES) });
    const response = await proxyToApi(request("PUT", json, tooLarge), ONE, undefined, api);
    expect(response.status).toBe(413);
    expect(api).not.toHaveBeenCalled();
  });

  it("never repeat or log a body, whatever becomes of the request", async () => {
    const logs = (["log", "info", "warn", "error", "debug"] as const).map((level) =>
      vi.spyOn(console, level).mockImplementation(() => undefined),
    );
    const down = vi.fn<typeof fetch>(async () => {
      throw new TypeError(`fetch failed for ${SECRET}`);
    });
    const answers = [
      // Forwarded and answered, refused by the API, and the API not answering.
      await proxyToApi(request("PUT", json, valueBody), ONE, undefined, apiAnswering(200, '{"source":"stored"}')),
      await proxyToApi(request("PUT", json, valueBody), ONE, undefined, apiAnswering(409, '{"code":"x"}')),
      await proxyToApi(request("PUT", json, valueBody), ONE, undefined, down),
      // Refused here: another site, no JSON, a query, an unknown path, a removal with a body.
      await proxyToApi(request("PUT", { ...json, origin: "https://evil.example" }, valueBody), ONE, undefined, down),
      await proxyToApi(request("PUT", { ...fromDashboard, "content-type": "text/plain" }, valueBody), ONE, undefined, down),
      await proxyToApi(request("PUT", json, valueBody, "?debug=1"), ONE, undefined, down),
      await proxyToApi(request("PUT", json, valueBody), [...ONE, "value"], undefined, down),
      await proxyToApi(request("DELETE", json, valueBody), ONE, undefined, down),
      await proxyToApi(request("PUT", json, valueBody), ONE, "not a url", down),
    ];
    for (const answer of answers) {
      expect(await answer.text()).not.toContain(SECRET);
      expect([...answer.headers.values()].join(" ")).not.toContain(SECRET);
    }
    for (const log of logs) expect(log).not.toHaveBeenCalled();
  });
});

describe("the API address", () => {
  it("defaults to the local API", () => {
    expect(apiBaseUrl(undefined).origin).toBe("http://127.0.0.1:8000");
    expect(apiBaseUrl("  ").origin).toBe("http://127.0.0.1:8000");
  });

  it("must be a plain http(s) origin", () => {
    for (const value of ["file:///etc", "http://user:pw@host", "http://host/base", "nonsense"]) {
      expect(() => apiBaseUrl(value)).toThrow("OPENMARKETER_API_URL");
    }
  });
});
