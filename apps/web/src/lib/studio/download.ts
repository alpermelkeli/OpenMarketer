/**
 * Saving a version's image as a file without leaving the page. The page fetches the image itself,
 * through its own proxy, and hands the bytes to the browser as a file: a link the browser follows
 * to the proxy is a navigation, which the proxy cannot tell from one another site starts, and it
 * refuses it. The file is named as the API names it, the version's id and the extension of its
 * type. This module does not fetch: the hook in `lib/api/studio.ts` does.
 */

// The same extensions the API gives the file it serves (`studio/asset_store.py`).
const EXTENSIONS: Readonly<Record<string, string>> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/webp": "webp",
};

/** The file's name: the version's id, and the extension of its type when the type is one the API stores. */
export function versionFileName(versionId: string, mime: string): string {
  const extension = EXTENSIONS[mime.trim().toLowerCase()];
  return extension === undefined ? versionId : `${versionId}.${extension}`;
}

/** What saving a file needs of the browser, so that the order of the steps can be tested without one. */
export type FileSaver = {
  createObjectURL: (file: Blob) => string;
  /** Follows a temporary `<a download>` to the object URL. */
  click: (href: string, fileName: string) => void;
  revokeObjectURL: (href: string) => void;
};

/** Hands the bytes to the browser as a file named `fileName`, and lets the object URL go afterwards. */
export function saveFile(file: Blob, fileName: string, saver: FileSaver = browserSaver()): void {
  const href = saver.createObjectURL(file);
  try {
    saver.click(href, fileName);
  } finally {
    saver.revokeObjectURL(href);
  }
}

function browserSaver(): FileSaver {
  return {
    createObjectURL: (file) => URL.createObjectURL(file),
    click: (href, fileName) => {
      const link = document.createElement("a");
      link.href = href;
      link.download = fileName;
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
    },
    // Later, not now: a browser may still be reading the URL when the click returns.
    revokeObjectURL: (href) => {
      setTimeout(() => URL.revokeObjectURL(href), 0);
    },
  };
}
