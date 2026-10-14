/** How a model of the provider's catalogue is described in the picker. Display only. */

import type { CatalogModel } from "@/lib/api/types";

const PRICE = new Intl.NumberFormat("en", { style: "currency", currency: "USD", maximumFractionDigits: 2 });

/** "$3 in, $15 out per million tokens", "free", or nothing when the catalogue gives no price. */
export function priceText(model: Pick<CatalogModel, "prompt_price" | "completion_price">): string | null {
  const { prompt_price: input, completion_price: output } = model;
  if (input === null || output === null) return null;
  if (input === 0 && output === 0) return "free";
  return `${PRICE.format(input)} in, ${PRICE.format(output)} out per million tokens`;
}

/** The text of a model's entry in the picker: its name, and its price when known. */
export function catalogEntryText(model: CatalogModel): string {
  const price = priceText(model);
  return price === null ? model.name : `${model.name} · ${price}`;
}
