"use client";

import { useModelCatalog } from "@/lib/api/model-settings";
import { catalogEntryText } from "@/lib/settings/model-format";

type ModelCatalogListProps = {
  /** The `list` of every model field that suggests from the catalogue. */
  id: string;
  /** Suggest only the models that make images, for a role of kind `images`. */
  imagesOnly?: boolean;
};

/**
 * The provider's models as suggestions for a model field. A field still takes any model
 * typed into it: the catalogue may be out of reach, and the API checks the form.
 */
export function ModelCatalogList({ id, imagesOnly = false }: ModelCatalogListProps) {
  const catalog = useModelCatalog();
  const models = catalog.data?.models ?? [];
  return (
    <datalist id={id}>
      {(imagesOnly ? models.filter(makesImages) : models).map((model) => (
        <option key={model.id} value={model.id}>
          {catalogEntryText(model)}
        </option>
      ))}
    </datalist>
  );
}

function makesImages(model: { output_modalities: readonly string[] }): boolean {
  return model.output_modalities.includes("image");
}

/** One line under the screen's heading on whether the picker can suggest models. */
export function ModelCatalogStatus() {
  const catalog = useModelCatalog();
  if (catalog.isPending) return null;
  if (catalog.data?.available) {
    return (
      <p className="text-sm text-muted-foreground">
        A model field suggests the {catalog.data.models.length} models of the provider&rsquo;s catalogue as you type.
      </p>
    );
  }
  return (
    <p className="text-sm text-muted-foreground">
      The provider&rsquo;s catalogue could not be read, so nothing is suggested and no model is checked. Type a model
      as <code>provider/model</code>; the names are listed at <span className="whitespace-nowrap">openrouter.ai/models</span>.
    </p>
  );
}
