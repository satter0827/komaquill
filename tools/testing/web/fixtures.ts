import { expect, type Page, type TestInfo } from '@playwright/test'
import path from 'node:path'
import { Evidence, hash } from './evidence.mjs'

export async function collectEvidence(
  { page }: { page: Page },
  use: (evidence: Evidence) => Promise<void>,
  info: TestInfo,
) {
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
  try {
    await use(evidence)
  } finally {
    await evidence.capture('Final visible state')
    const result = await evidence.finish(info.status, info.expectedStatus)
    await info.attach('Evidence manifest', {
      path: path.join(directory, 'scenario.json'),
      contentType: 'application/json',
    })
    if (process.env.KOMAQUILL_WEB_MODE !== 'exploration') {
      expect(result.unexpected_events).toEqual([])
    }
  }
}
