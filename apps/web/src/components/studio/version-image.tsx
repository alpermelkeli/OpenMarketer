import { ImageOffIcon } from "lucide-react";
import Image from "next/image";

import { versionImageSrc } from "@/lib/studio/image-src";
import { cn } from "@/lib/utils";

type VersionImageProps = {
  projectId: string;
  assetId: string;
  versionId: string;
  alt: string;
  /** The image's own size, when the version is known: it is shown at its shape, never cropped. */
  size?: { width: number; height: number };
  /** How wide it is shown, for the browser to choose when to load it. */
  sizes: string;
  className?: string;
};

/**
 * One version's image, read from the dashboard's own address for it. Without the version's size it
 * fills a square frame, contained. It is not optimised by Next.js: the proxy serves it as stored.
 */
export function VersionImage({ projectId, assetId, versionId, alt, size, sizes, className }: VersionImageProps) {
  const src = versionImageSrc({ projectId, assetId, versionId });
  if (src === null) {
    return (
      <div className={cn("grid aspect-square place-items-center bg-surface text-muted-foreground", className)}>
        <ImageOffIcon aria-hidden="true" className="size-5" />
        <span className="sr-only">This image has no address the dashboard can read.</span>
      </div>
    );
  }
  if (size === undefined) {
    return (
      <div className={cn("relative aspect-square bg-surface", className)}>
        <Image src={src} alt={alt} fill sizes={sizes} unoptimized className="object-contain" />
      </div>
    );
  }
  return (
    <Image
      src={src}
      alt={alt}
      width={size.width}
      height={size.height}
      sizes={sizes}
      unoptimized
      className={cn("h-auto w-full bg-surface", className)}
    />
  );
}
