// Auth-mode catalogue for the MCP dialog (spec D.2 / D-M2). The enum
// will grow — new modes land as a data-only addition here with zero
// layout work; the dialog renders the select + dynamic hint from this
// list. Semantics mirror the wire `MCPAuthMode` docs in openapi.yaml.

import type { MCPAuthMode } from '../../../api/client';

export interface AuthModeOption {
  id: MCPAuthMode;
  label: string;
  description: string;
}

export const AUTH_MODES: readonly AuthModeOption[] = [
  {
    id: 'service',
    label: 'Service',
    description:
      'A shared service credential from the vault — the credential proxy injects it; agents never see it.',
  },
  {
    id: 'user',
    label: 'User',
    description: "The requesting user's own credential is used for each call.",
  },
  {
    id: 'both',
    label: 'Both',
    description: 'User credential when present, service credential as fallback.',
  },
];

export function authModeDescription(mode: MCPAuthMode): string {
  return AUTH_MODES.find((m) => m.id === mode)?.description ?? '';
}
