import { afterEach, describe, expect, it, vi } from "vitest";

import { MAX_BODY_BYTES, allowedApiTarget, apiBaseUrl, assertFromDashboard, proxyToApi } from "./api-proxy";

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

  it("forward the list, an approval and a rejection", () => {
    expect(allowedApiPath("GET", ["projects", PROJECT, "proposals"])).toBe(`/v1/projects/${PROJECT}/proposals`);
    expect(allowedApiPath("POST", proposal("approval"))).toBe(`/v1/projects/${PROJECT}/proposals/${RUN}/approval`);
    expect(allowedApiPath("POST", proposal("rejection"))).toBe(`/v1/projects/${PROJECT}/proposals/${RUN}/rejection`);
  });

  it("do not include a decision by GET, a proposal that is not an id, or a filter", () => {
    expect(() => allowedApiPath("GET", proposal("approval"))).toThrow();
    expect(() => allowedApiPath("POST", ["projects", PROJECT, "proposals", "latest", "approval"])).toThrow();
    expect(() => allowedApiPath("POST", ["projects", PROJECT, "proposals", RUN])).toThrow();
    expect(() =>
      allowedApiTarget("GET", ["projects", PROJECT, "proposals"], new URLSearchParams("status=pending")),
    ).toThrow();
  });
});

describe("the routes of a project's chat", () => {
  const CHAT = "0b0e6c6e-aaaa-4bbb-8ccc-ddddeeeeffff";
  const CHATS = ["projects", PROJECT, "chats"];

  it("forward the chats, one of them, its messages and its read mark", () => {
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
  });

  it("do not include a wrong method, a chat that is not an id, or a path below one", () => {
    expect(() => allowedApiPath("GET", [...CHATS, CHAT, "read"])).toThrow();
    expect(() => allowedApiPath("POST", [...CHATS, CHAT])).toThrow();
    expect(() => allowedApiPath("POST", [...CHATS, CHAT, "messages", RUN])).toThrow();
    expect(() => allowedApiPath("GET", [...CHATS, "latest"])).toThrow();
    expect(() => allowedApiPath("GET", ["projects", PROJECT, "chat"])).toThrow();
    expect(() =>
      allowedApiTarget("GET", [...CHATS, CHAT, "messages"], new URLSearchParams("role=user")),
    ).toThrow();
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
