import { describe, expect, it } from 'vitest';
import { ApiError } from '../../../api/client';
import { deleteErrText } from './delete-error';

describe('deleteErrText', () => {
  it('renders a 409 RESOURCE_IN_USE as a coworker count from details', () => {
    const err = new ApiError(
      409,
      { code: 'RESOURCE_IN_USE', message: 'in use', details: { coworker_ids: ['a', 'b', 'c'] } },
      'in use',
    );
    expect(deleteErrText(err)).toBe(
      'In use by 3 coworkers — unbind it from each before deleting.',
    );
  });

  it('singularizes at one bound coworker', () => {
    const err = new ApiError(
      409,
      { code: 'RESOURCE_IN_USE', message: 'in use', details: { coworker_ids: ['a'] } },
      'in use',
    );
    expect(deleteErrText(err)).toContain('In use by 1 coworker —');
  });

  it('falls back to the message when 409 has no coworker_ids', () => {
    const err = new ApiError(409, { code: 'CONFLICT', message: 'boom' }, 'boom');
    expect(deleteErrText(err)).toBe('boom');
  });

  it('uses the message for non-409 ApiErrors and plain errors', () => {
    expect(deleteErrText(new ApiError(500, { code: 'X', message: 'server' }, 'server'))).toBe(
      'server',
    );
    expect(deleteErrText(new Error('network'))).toBe('network');
  });
});
