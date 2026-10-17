import { describe, expect, it } from "vitest";

import { catalogEntryText, priceText } from "./model-format";

const MODEL = {
  id: "anthropic/claude-sonnet-5.5",
  name: "Claude Sonnet 5.5",
  context_length: 200000,
  prompt_price: 3,
  completion_price: 15,
  supported_parameters: ["tools"],
  input_modalities: ["text"],
  output_modalities: ["text"],
  image_output_price: null,
};

describe("a model in the picker", () => {
  it("is shown with its price per million tokens", () => {
    expect(catalogEntryText(MODEL)).toBe("Claude Sonnet 5.5 · $3.00 in, $15.00 out per million tokens");
  });

  it("is said to be free when both prices are zero", () => {
    expect(priceText({ prompt_price: 0, completion_price: 0 })).toBe("free");
  });

  it("is shown by its name alone when the catalogue gives no price", () => {
    expect(catalogEntryText({ ...MODEL, prompt_price: null })).toBe("Claude Sonnet 5.5");
  });
});
