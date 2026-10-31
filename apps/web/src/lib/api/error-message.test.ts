import { describe, expect, it } from "vitest";

import { ApiError } from "./api-error";
import { errorMessage } from "./error-message";

describe("a refused deletion of a run a post uses", () => {
  it("says the studio run is kept as long as its post", () => {
    const error = new ApiError(409, "studio_run_used_by_post", "studio run 1 made the image of a post");
    expect(errorMessage(error)).toMatch(/kept as long as the post is/);
  });

  it("says the research run is kept as long as its post", () => {
    const error = new ApiError(409, "research_used_by_post", "research run 1 informed a post");
    expect(errorMessage(error)).toMatch(/kept as long as the post is/);
  });
});

describe("a failure the dashboard has no advice for", () => {
  it("is the API's own message", () => {
    expect(errorMessage(new ApiError(503, "storage_unavailable", "Object storage is down."))).toBe(
      "Object storage is down.",
    );
  });
});
