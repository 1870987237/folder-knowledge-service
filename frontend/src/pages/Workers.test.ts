import { describe, expect, it } from 'vitest';
import { retryRequest } from './Workers';

describe('worker retry actions', () => {
  const failure = { id: 'job-id', resource_id: 'library-or-repo', resource_name: 'Example', error: 'failed', failed_at: '' };
  it('reuses the repository sync endpoint', () => {
    expect(retryRequest('git', failure)).toEqual(['/repositories/library-or-repo/sync', undefined]);
  });
  it('retries the failed update job instead of creating an unrelated update', () => {
    expect(retryRequest('updates', failure)).toEqual(['/update-jobs/job-id/retry', undefined]);
  });
});
