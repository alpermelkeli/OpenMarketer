import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { BusinessModelType, ProductProfile } from "@/lib/api/types";
import { featuresInReviewOrder } from "@/lib/profile/attention";
import type { EvidenceSource } from "@/lib/profile/evidence-link";
import {
  withAudiencePrimary,
  withBrandVoice,
  withBusinessModelType,
  withFeature,
  withProductName,
} from "@/lib/profile/edits";
import { BUSINESS_MODEL_TYPES, businessModelLabel, humanizeSlug } from "@/lib/profile/labels";

import { FeatureItem } from "./feature-item";
import { OptionSelect } from "./option-select";
import { PaletteSwatches } from "./palette-swatches";
import { ProfileSection } from "./profile-section";
import { NotFound, ProfileValue, TagList, TextList, TextValue } from "./profile-value";

const BUSINESS_MODEL_OPTIONS = BUSINESS_MODEL_TYPES.map((type) => ({
  value: type,
  label: businessModelLabel(type),
}));

type ProfileDocumentProps = {
  profile: ProductProfile;
  source: EvidenceSource;
  /** While editing, values become fields and every change is reported through `onChange`. */
  editing: boolean;
  onChange: (profile: ProductProfile) => void;
};

/** The whole Product Profile, section by section. Everything in it is untrusted text. */
export function ProfileDocument({ profile, source, editing, onChange }: ProfileDocumentProps) {
  const { product, brand, audience, business_model: businessModel, measurement } = profile;
  const features = featuresInReviewOrder(profile.features ?? []);

  return (
    <div className="grid gap-section">
      <ProfileSection
        section="product"
        title="Product"
        purpose="What it is and where it runs."
        claim={product}
        source={source}
      >
        <ProfileValue label="Name">
          {editing ? (
            <Input
              aria-label="Product name"
              value={product.name}
              onChange={(event) => onChange(withProductName(profile, event.target.value))}
              className="max-w-80"
            />
          ) : (
            <TextValue text={product.name} />
          )}
        </ProfileValue>
        <ProfileValue label="Type">
          {humanizeSlug(product.type)} <span className="font-mono text-xs text-muted-foreground">{product.type}</span>
        </ProfileValue>
        <ProfileValue label="Platforms">
          <TagList values={product.platforms} />
        </ProfileValue>
        <ProfileValue label="Languages">
          <TagList values={product.languages} />
        </ProfileValue>
      </ProfileSection>

      <section aria-labelledby="features-heading" className="grid gap-6 border-t pt-8">
        <div>
          <h3 id="features-heading" className="font-heading text-title">
            Features
            <span className="ml-2 font-sans text-sm text-muted-foreground tabular-nums">{features.length}</span>
          </h3>
          <p className="max-w-prose text-sm text-pretty text-muted-foreground">
            Only features marked Live may be mentioned publicly. Those needing a decision come first.
          </p>
        </div>
        {features.length === 0 ? (
          <p className="text-sm text-muted-foreground">The analyzer listed no features.</p>
        ) : (
          <ul className="divide-y">
            {features.map((feature) => (
              <FeatureItem
                key={feature.id}
                feature={feature}
                source={source}
                editing={editing}
                onChange={(change) => onChange(withFeature(profile, feature.id, change))}
              />
            ))}
          </ul>
        )}
      </section>

      <ProfileSection
        section="brand"
        title="Brand"
        purpose="How the product speaks and looks."
        claim={brand}
        source={source}
      >
        <ProfileValue label="Voice">
          {editing ? (
            <Textarea
              aria-label="Brand voice"
              value={brand?.voice ?? ""}
              onChange={(event) => onChange(withBrandVoice(profile, event.target.value))}
              className="max-w-prose"
            />
          ) : (
            <TextValue text={brand?.voice} />
          )}
        </ProfileValue>
        <ProfileValue label="Palette">
          <PaletteSwatches palette={brand?.palette} />
        </ProfileValue>
        <ProfileValue label="Do not mention">
          <TextList values={brand?.do_not_mention} />
        </ProfileValue>
      </ProfileSection>

      <ProfileSection
        section="audience"
        title="Audience"
        purpose="Who it is for and what troubles them."
        claim={audience}
        source={source}
      >
        <ProfileValue label="Primary audience">
          {editing ? (
            <Textarea
              aria-label="Primary audience"
              value={audience?.primary ?? ""}
              onChange={(event) => onChange(withAudiencePrimary(profile, event.target.value))}
              className="max-w-prose"
            />
          ) : (
            <TextValue text={audience?.primary} />
          )}
        </ProfileValue>
        <ProfileValue label="Pains">
          <TextList values={audience?.pains} />
        </ProfileValue>
      </ProfileSection>

      <ProfileSection
        section="business_model"
        title="Business model"
        purpose="How the product earns."
        claim={businessModel}
        source={source}
      >
        <ProfileValue label="Type">
          {editing && businessModel !== undefined ? (
            <OptionSelect<BusinessModelType>
              label="Business model type"
              value={businessModel.type}
              options={BUSINESS_MODEL_OPTIONS}
              onChange={(type) => onChange(withBusinessModelType(profile, type))}
            />
          ) : businessModel === undefined ? (
            <NotFound />
          ) : (
            businessModelLabel(businessModel.type)
          )}
        </ProfileValue>
        <ProfileValue label="Trial">
          {typeof businessModel?.trial_days === "number" ? `${businessModel.trial_days} days` : <NotFound />}
        </ProfileValue>
      </ProfileSection>

      <ProfileSection
        section="measurement"
        title="Measurement"
        purpose="What the product can already measure."
        claim={measurement}
        source={source}
      >
        <ProfileValue label="Analytics">
          <TagList values={measurement?.analytics} />
        </ProfileValue>
        <ProfileValue label="Attribution">
          <TextValue text={measurement?.attribution} />
        </ProfileValue>
        <ProfileValue label="Deep links">
          {typeof measurement?.deep_links === "boolean" ? measurement.deep_links ? "Yes" : "No" : <NotFound />}
        </ProfileValue>
      </ProfileSection>
    </div>
  );
}
