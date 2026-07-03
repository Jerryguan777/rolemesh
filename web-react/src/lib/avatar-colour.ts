// Deterministic identification colour for an agent id (prototype's
// colourFor). Returns a CSS var reference into the tokenized
// --rm-avatar-* palette so components stay hex-free.

const PALETTE_SIZE = 6;

export function avatarColourVar(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  return `var(--rm-avatar-${Math.abs(h) % PALETTE_SIZE})`;
}
