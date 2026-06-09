/// <reference types="vite/client" />

// Vite injects `import.meta.env` at build time; this reference makes the
// `ImportMeta.env` typings available to `tsc --noEmit` over the whole
// project (vite build only typechecks the app entry, so without this the
// env access in e.g. chat-shell.ts is untyped under a full typecheck).
