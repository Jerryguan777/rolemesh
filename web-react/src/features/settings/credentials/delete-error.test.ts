import { describe, expect, it } from 'vitest';
import { ApiError } from '../../../api/client';
import { credDeleteErrText } from './delete-error';

describe('credDeleteErrText', () => {
  it('surfaces the 409 RESOURCE_IN_USE coworker count', () => {
    const err = new ApiError(
      409,
      { code: 'RESOURCE_IN_USE', message: 'in use', details: { coworker_ids: ['a', 'b', 'c'] } },
      'in use',
    );
    expect(credDeleteErrText(err)).toBe(
      'This credential is in use by 3 coworker(s). Detach them before deleting.',
    );
  });

  it('falls back to the message when details lack coworker_ids', () => {
    const err = new ApiError(400, { code: 'BAD', message: 'nope' }, 'nope');
    expect(credDeleteErrText(err)).toBe('nope');
  });

  it('falls back to status when no message', () => {
    const err = new ApiError(500, null, '');
    expect(credDeleteErrText(err)).toBe('500');
  });

  it('handles non-ApiError', () => {
    expect(credDeleteErrText(new Error('boom'))).toBe('boom');
  });
});
