import { describe, expect, it, vi } from "vitest";

import { allowedApiTarget, apiBaseUrl, assertFromDashboard, proxyToApi } from "./api-proxy";

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
    expect(() => allowedApiPath("DELETE", ["projects", PROJECT])).toThrow();
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
