// Thin typed wrapper over the v1.1 OpenAPI contract. Intentionally
// minimal — frontends should always call through this module rather
// than hand-writing fetch URLs, so adding a new endpoint to the yaml
// (and re-running `npm run openapi:gen`) is the only way to grow the
// client surface. If you find yourself reaching for `fetch` directly
// from a component, that's a smell: extend this file instead.
//
// Why no fancy runtime path-builder: TS path-template inference adds
// a lot of complexity for marginal value. We hand-write one method
// per endpoint we actually need; the input/output shapes flow from
// `paths` so a yaml change shows up as a type error here.

import type { paths } from './generated/types.js';

export type ApiPaths = paths;

type GetResponseBody<
  P extends keyof paths,
  M extends keyof paths[P],
> = paths[P][M] extends {
  responses: { 200: { content: { 'application/json': infer B } } };
}
  ? B
  : never;

export type BackendList = GetResponseBody<'/api/v1/backends', 'get'>;
export type ErrorResponseBody =
  paths['/api/v1/runs/{id}/cancel']['post']['responses']['409']['content']['application/json'];

export class ApiError extends Error {
  readonly status: number;
  readonly body: ErrorResponseBody | null;
  constructor(status: number, body: ErrorResponseBody | null, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export class ApiClient {
  private token: string | null;

  constructor(
    private readonly baseUrl: string = '',
    token: string | null = null,
  ) {
    this.token = token;
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  private headers(extra?: Record<string, string>): Record<string, string> {
    const h: Record<string, string> = { Accept: 'application/json' };
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    if (extra) Object.assign(h, extra);
    return h;
  }

  private async parseError(resp: Response): Promise<ApiError> {
    let body: ErrorResponseBody | null = null;
    try {
      body = (await resp.json()) as ErrorResponseBody;
    } catch {
      // Not JSON — fall through to status-only error.
    }
    const msg = body?.message || `HTTP ${resp.status}`;
    return new ApiError(resp.status, body, msg);
  }

  async getBackends(): Promise<BackendList> {
    const resp = await fetch(`${this.baseUrl}/api/v1/backends`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (!resp.ok) throw await this.parseError(resp);
    return (await resp.json()) as BackendList;
  }
}
