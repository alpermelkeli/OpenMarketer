import { describe, expect, it, vi } from "vitest";

import { allowedApiPath, apiBaseUrl, assertFromDashboard, proxyToApi } from "./api-proxy";

const PROJECT = "8ce5e2bb-6e54-49b4-9ddc-b96f20ec1109";
const RUN = "0b0e6c6e-1111-4222-8333-444455556666";
const APPROVAL = ["projects", PROJECT, "profile", "versions", "3", "approval"];

function request(method: string, headers: Record<string, string>, body?: string): Request {
  return new Request("http://localhost:3000/api/v1/ignored", { method, headers, body });
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
      ["projects", PROJECT, "profile", "draft"],
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
    expect(() => allowedApiPath("GET", ["projects"])).toThrow();
    expect(() => allowedApiPath("DELETE", ["projects", PROJECT, "profile", "draft"])).toThrow();
    expect(() => allowedApiPath("GET", APPROVAL)).toThrow();
  });

  it("do not include paths outside the list", () => {
    for (const segments of [
      ["health"],
      ["projects", "not-a-uuid", "profile", "draft"],
      ["projects", PROJECT, "profile", "versions", "0", "approval"],
      ["projects", PROJECT, "profile", "versions", "1e3", "approval"],
      ["projects", PROJECT, "profile", "draft", ".."],
      ["projects", `${PROJECT}/profile/draft?x=`, "profile", "draft"],
      ["..", "..", "admin"],
      ["projects", `${PROJECT}\n`, "profile", "draft"],
    ]) {
      expect(() => allowedApiPath("GET", segments)).toThrow();
    }
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
