import fs from 'node:fs'
import path from 'node:path'

import { expect, test } from '@playwright/test'

import { expectVisualSnapshot, visualCandidateCaptureEnabled } from './visual-snapshot'

test('allows candidate capture only when the protected workflow explicitly enables it', () => {
  expect(visualCandidateCaptureEnabled('all', '1')).toBe(true)
  expect(visualCandidateCaptureEnabled('changed', '1')).toBe(true)
  expect(visualCandidateCaptureEnabled('all')).toBe(false)
  expect(visualCandidateCaptureEnabled('none', '1')).toBe(false)
})

test('requires an approved baseline instead of accepting a first-run screenshot', async () => {
  const page = {
    addStyleTag: async () => undefined,
    screenshot: async () => Buffer.from('new visual output'),
    waitForTimeout: async () => undefined
  }
  const app = {
    evaluate: async () => undefined
  }

  await expect(
    expectVisualSnapshot(page as never, {
      name: 'visual-snapshot-policy-missing-baseline',
      app: app as never
    })
  ).rejects.toThrow('approved visual baseline')
})

test('masks volatile toast and placeholder text before visual comparison', async () => {
  const styles: unknown[] = []
  const page = {
    addStyleTag: async (style: unknown) => {
      styles.push(style)
    },
    screenshot: async () => Buffer.from('new visual output'),
    waitForTimeout: async () => undefined
  }
  const app = {
    evaluate: async () => undefined
  }

  await expect(
    expectVisualSnapshot(page as never, {
      name: 'visual-snapshot-policy-stabilized-state',
      app: app as never
    })
  ).rejects.toThrow('approved visual baseline')

  expect(styles).toContainEqual({
    content: expect.stringContaining('[role="status"]')
  })
})

test('fails and preserves review artifacts when pixels differ from the approved baseline', async ({}, testInfo) => {
  const name = 'visual-snapshot-policy-mismatch'
  const baselinePath = testInfo.snapshotPath(`${name}.png`)
  fs.mkdirSync(path.dirname(baselinePath), { recursive: true })
  fs.writeFileSync(baselinePath, Buffer.from('approved visual output'))

  const page = {
    addStyleTag: async () => undefined,
    screenshot: async () => Buffer.from('changed visual output'),
    waitForTimeout: async () => undefined
  }
  let evaluateCalls = 0
  const app = {
    evaluate: async () => {
      evaluateCalls += 1
      return evaluateCalls === 1
        ? undefined
        : { mismatchRatio: 0.25, diff: Buffer.from('visual diff').toString('base64') }
    }
  }

  try {
    await expect(
      expectVisualSnapshot(page as never, {
        name,
        app: app as never
      })
    ).rejects.toThrow('visual-regression')
  } finally {
    fs.rmSync(baselinePath, { force: true })
  }
})
