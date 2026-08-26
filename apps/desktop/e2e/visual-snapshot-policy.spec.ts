import { expect, test } from '@playwright/test'

import { expectVisualSnapshot } from './visual-snapshot'

test('requires an approved baseline instead of accepting a first-run screenshot', async () => {
  const page = {
    screenshot: async () => Buffer.from('new visual output'),
    waitForTimeout: async () => undefined,
  }
  const app = {
    evaluate: async () => undefined,
  }

  await expect(
    expectVisualSnapshot(page as never, {
      name: 'visual-snapshot-policy-missing-baseline',
      app: app as never,
    }),
  ).rejects.toThrow('approved visual baseline')
})
