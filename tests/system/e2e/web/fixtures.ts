import { test as base, expect } from '@playwright/test'
import type { Route } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import {
  Evidence,
  route as safeRoute,
} from '../../../../tools/testing/web/evidence.mjs'
import AxeBuilder from '@axe-core/playwright'
import { collectEvidence } from '../../../../tools/testing/web/fixtures'
import { isolatedServer } from '../../../../tools/testing/web/server-fixture'

export const test = base.extend<{
  archive: boolean
  evidence: Evidence
  auditAccessibility: () => Promise<void>
}>({
  archive: [false, { option: true }],
  baseURL: async ({ archive }, use, info) => {
    await isolatedServer(use, info, archive)
  },
  request: async ({ playwright, baseURL }, use) => {
    const request = await playwright.request.newContext({
      baseURL,
      extraHTTPHeaders: { Connection: 'close' },
    })
    try {
      await use(request)
    } finally {
      await request.dispose()
    }
  },
  auditAccessibility: async ({ page }, use, info) => {
    let audit = 0
    await use(async () => {
      const result = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
        .analyze()
      const resultPath = info.outputPath(`accessibility-${audit++}.json`)
      await writeFile(
        resultPath,
        JSON.stringify(
          {
            url: page.url(),
            engine: result.testEngine,
            timestamp: result.timestamp,
            violations: result.violations,
            incomplete: result.incomplete,
            status: result.violations.length
              ? 'failed'
              : result.incomplete.length
                ? 'manual_review_required'
                : 'passed',
          },
          null,
          2,
        ),
      )
      await info.attach('Accessibility results', {
        path: resultPath,
        contentType: 'application/json',
      })
      expect(result.violations).toEqual([])
    })
  },
  evidence: [
    async ({ page }, use, info) => {
      await collectEvidence({ page }, use, info)
    },
    { auto: true },
  ],
})
export { expect }

export async function fulfillFault(
  route: Route,
  evidence: Evidence,
  options: Parameters<Route['fulfill']>[0] & { status: number },
) {
  evidence.expectFailure({
    kind: 'response',
    route: safeRoute(route.request().url()),
    method: route.request().method(),
    status: options.status,
    maximum: 1,
  })
  await route.fulfill(options)
}

export async function abortFault(route: Route, evidence: Evidence) {
  evidence.expectFailure({
    kind: 'request_failed',
    route: safeRoute(route.request().url()),
    method: route.request().method(),
    maximum: 1,
  })
  await route.abort('failed')
}
