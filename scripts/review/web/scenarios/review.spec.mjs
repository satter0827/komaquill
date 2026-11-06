import { test as base } from '@playwright/test'
import { collectEvidence } from '../../../../tools/testing/web/fixtures.ts'
import { isolatedServer } from '../../../../tools/testing/web/server-fixture.ts'
import { captureJourney } from '../../../../tools/testing/web/journey.mjs'

const test = base.extend({
  baseURL: async ({}, use, info) => isolatedServer(use, info),
  evidence: [collectEvidence, { auto: true }],
})

test('Capture the creation and conversation journey for human or AI review', async ({
  page,
  evidence,
}) => {
  await captureJourney(page, evidence)
})
