"use client";

import Image from "next/image";
import type { ReactNode } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

type ImageViewerDialogProps = {
  /** What the person clicks to open the viewer: the thumbnail and its caption. */
  children: ReactNode;
  /** The address the dashboard built for the image; it is read as an image, never navigated to. */
  src: string;
  alt: string;
  /** What kind of image it is, in words: the viewer's title. */
  kind: string;
  /** Where the image is in the repository, or the name it was uploaded as, shown as text. */
  path: string;
  /** Its size in words, as "1280 × 720 px". */
  size: string;
  width: number;
  height: number;
};

/**
 * One image at full size, over the page, with its kind, path and size. It opens from its
 * thumbnail and closes with its button, Escape or a click outside; focus goes back to the
 * thumbnail. The image is loaded from the same address as the thumbnail, so it stays on the page:
 * a new tab would be a navigation to the proxy, which the proxy refuses.
 */
export function ImageViewerDialog({ children, src, alt, kind, path, size, width, height }: ImageViewerDialogProps) {
  return (
    <Dialog>
      <DialogTrigger className="grid w-full gap-2 rounded-sm text-left outline-offset-4 transition-opacity hover:opacity-85">
        {children}
        <span className="sr-only">, opens the image at full size</span>
      </DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-2rem)] grid-rows-[auto_minmax(0,1fr)] rounded-md sm:max-w-3xl">
        <DialogHeader className="min-w-0 pr-8">
          <DialogTitle className="text-2xs font-normal tracking-widest text-muted-foreground uppercase">
            {kind}
          </DialogTitle>
          <DialogDescription className="grid gap-0.5">
            <span className="block truncate font-mono text-xs text-foreground" title={path}>
              {path}
            </span>
            <span className="block font-mono text-xs tabular-nums">{size}</span>
          </DialogDescription>
        </DialogHeader>
        <div className="grid min-h-0 place-items-center overflow-auto rounded-sm border bg-surface">
          <Image
            src={src}
            alt={alt}
            width={width}
            height={height}
            sizes="(min-width: 640px) 48rem, 100vw"
            unoptimized
            className="h-auto max-h-[calc(100dvh-10rem)] w-auto max-w-full object-contain"
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}
