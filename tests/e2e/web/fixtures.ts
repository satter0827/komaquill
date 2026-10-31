import { test as base, expect } from '@playwright/test'
import path from 'node:path'
import { readFile } from 'node:fs/promises'
import { Evidence, hash } from '../../../scripts/web/evidence.mjs'

export const test = base.extend<{ evidence: Evidence }>({
  evidence: [
    async ({ page, request }, use, info) => {
      const directory = path.join(
        process.env.KOMAQUILL_WEB_EVIDENCE_DIR!,
        'scenarios',
        `${hash(info.testId).slice(0, 20)}-${info.retry}`,
      )
      const evidence = new Evidence(page, directory, {
        mode: process.env.KOMAQUILL_WEB_MODE,
        title: info.title,
        test_id: info.testId,
        viewport: page.viewportSize(),
      })
      const current = await (await request.get('/api/v1/preferences')).json()
      const fixture = JSON.parse(
        await readFile(
          path.join(process.env.KOMAQUILL_WEB_EVIDENCE_DIR!, 'fixture.json'),
          'utf8',
        ),
      )
      const reset = await request.put('/api/v1/preferences', {
        data: {
          ...fixture.initial_preferences,
          revision: current.revision,
        },
      })
      expect(reset.ok()).toBe(true)
      try {
        await use(evidence)
      } finally {
        await evidence.capture('Final visible state')
        await evidence.finish(info.status, info.expectedStatus)
        await info.attach('Evidence manifest', {
          path: path.join(directory, 'scenario.json'),
          contentType: 'application/json',
        })
      }
    },
    { auto: true },
  ],
})
export { expect }
