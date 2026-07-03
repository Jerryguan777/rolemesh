// DOM file-reading for the skill upload zone — the impure edge the pure
// ingestUploads (lib/skill-upload) can't cover. Two entry points mirror
// the Lit dialog's path policy exactly:
//
//   - readFilesFromInput  (folder/file <input>): preserve
//     webkitRelativePath as-is (PR27 — users pick sub-folders like
//     `references/` and expect that name to survive; a wrapper folder
//     is fixed by picking its children).
//   - readDroppedItems (drag-drop): recursive webkitGetAsEntry walk
//     where each dropped item's OWN name stays as the path prefix
//     (PR26 — `references/intro.md` must not flatten to `intro.md`);
//     getAsFile fallback when the entry API is missing.
//
// Unreadable files become `\0` content so the binary gate in
// ingestUploads catches + tallies them.

import type { IncomingFile } from '../../../lib/skill-upload';

// Minimal structural typing for the non-standard FileSystemEntry API
// (lib.dom.d.ts coverage varies by tsconfig target; pin the shape used).
interface FileSystemEntryLike {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
}
interface FileSystemFileEntryLike extends FileSystemEntryLike {
  file: (cb: (f: File) => void, errcb?: (e: unknown) => void) => void;
}
interface FileSystemDirectoryEntryLike extends FileSystemEntryLike {
  createReader: () => {
    readEntries: (
      cb: (entries: FileSystemEntryLike[]) => void,
      errcb?: (e: unknown) => void,
    ) => void;
  };
}

function readFileAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onerror = () => reject(r.error ?? new Error('read error'));
    r.onload = () => {
      const v = r.result;
      if (typeof v !== 'string') {
        reject(new Error('expected text content'));
        return;
      }
      resolve(v);
    };
    r.readAsText(file);
  });
}

/** Picker input → (path, content, bytes). webkitRelativePath is
 *  "folder/sub/file.md" for the folder picker, empty for plain files. */
export async function readFilesFromInput(list: FileList): Promise<IncomingFile[]> {
  const out: IncomingFile[] = [];
  for (const file of Array.from(list)) {
    const path =
      (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
    try {
      const content = await readFileAsText(file);
      out.push({ path, content, bytes: file.size });
    } catch {
      out.push({ path, content: '\0', bytes: file.size });
    }
  }
  return out;
}

/** Drag-drop → recursive webkitGetAsEntry walk. Each dropped item's own
 *  name becomes the path prefix so folder structure survives. */
export async function readDroppedItems(
  items: DataTransferItemList,
): Promise<IncomingFile[]> {
  const out: IncomingFile[] = [];
  const entries: FileSystemEntryLike[] = [];
  for (const it of Array.from(items)) {
    if (it.kind !== 'file') continue;
    const getEntry = (
      it as DataTransferItem & { webkitGetAsEntry?: () => FileSystemEntryLike | null }
    ).webkitGetAsEntry;
    const entry = typeof getEntry === 'function' ? getEntry.call(it) : null;
    if (entry) {
      entries.push(entry);
    } else {
      // No entry API (mostly happy-dom in tests) — fall back to getAsFile.
      const f = it.getAsFile();
      if (f) {
        const content = await readFileAsText(f).catch(() => '\0');
        out.push({ path: f.name, content, bytes: f.size });
      }
    }
  }

  async function walk(entry: FileSystemEntryLike, prefix: string): Promise<void> {
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) =>
        (entry as FileSystemFileEntryLike).file(resolve, reject),
      );
      const content = await readFileAsText(file).catch(() => '\0');
      // Preserve the full structural prefix (PR26: no flattening).
      out.push({ path: prefix + file.name, content, bytes: file.size });
    } else if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntryLike).createReader();
      // readEntries returns a batch at a time; keep calling until empty.
      let batch: FileSystemEntryLike[] = [];
      do {
        batch = await new Promise<FileSystemEntryLike[]>((resolve, reject) =>
          reader.readEntries(resolve, reject),
        );
        for (const child of batch) {
          await walk(child, `${prefix}${entry.name}/`);
        }
      } while (batch.length > 0);
    }
  }

  for (const e of entries) {
    await walk(e, '');
  }
  return out;
}
