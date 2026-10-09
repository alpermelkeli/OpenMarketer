import { NotFound } from "./profile-value";

const HEX_COLOR = /^#[0-9A-Fa-f]{6}$/;

type PaletteSwatchesProps = { palette: readonly string[] | undefined };

/**
 * The brand colours the analyzer found. A value goes into a style only if it is a
 * plain six-digit hex colour; anything else is shown as text without a swatch.
 */
export function PaletteSwatches({ palette }: PaletteSwatchesProps) {
  if (palette === undefined || palette.length === 0) return <NotFound />;
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-2">
      {palette.map((color, index) => (
        <li key={`${color}:${index}`} className="flex items-center gap-2 font-mono text-xs">
          {HEX_COLOR.test(color) && (
            <span
              aria-hidden="true"
              className="size-4 rounded-sm border border-border-strong"
              style={{ backgroundColor: color }}
            />
          )}
          {color}
        </li>
      ))}
    </ul>
  );
}
