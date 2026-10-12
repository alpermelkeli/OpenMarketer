import { MutationObserver, QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { OneTimeValue } from "./one-time-value";

// Not a real credential: a marker that must not be left anywhere.
const SECRET = "sk-or-a-value-that-must-not-stay";

describe("a value that is sent once", () => {
  it("is given out once", () => {
    const held = new OneTimeValue(SECRET);
    expect(held.take()).toBe(SECRET);
    expect(() => held.take()).toThrow("already been sent");
  });

  it("is not part of what the holder shows of itself", () => {
    const held = new OneTimeValue(SECRET);
    expect(JSON.stringify(held)).toBe("{}");
    expect(Object.keys(held)).toEqual([]);
    expect(String(held)).not.toContain(SECRET);
  });

  it("is not in the mutation cache once the request has been made, whether it succeeded or failed", async () => {
    for (const succeeds of [true, false]) {
      const queryClient = new QueryClient();
      const sent: string[] = [];
      const mutation = new MutationObserver<null, Error, OneTimeValue>(queryClient, {
        mutationFn: async (held) => {
          sent.push(held.take());
          if (!succeeds) throw new Error("refused");
          return null;
        },
      });
      await mutation.mutate(new OneTimeValue(SECRET)).catch(() => undefined);

      expect(sent).toEqual([SECRET]);
      const [cached] = queryClient.getMutationCache().getAll();
      expect(cached.state.status).toBe(succeeds ? "success" : "error");
      expect(() => (cached.state.variables as OneTimeValue).take()).toThrow("already been sent");
      expect(JSON.stringify(cached.state)).not.toContain(SECRET);
      expect(JSON.stringify(mutation.getCurrentResult())).not.toContain(SECRET);
    }
  });
});
