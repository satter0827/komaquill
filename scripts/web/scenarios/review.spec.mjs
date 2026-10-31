import { test } from '../../../tests/e2e/web/fixtures.ts'
import { captureJourney } from '../journey.mjs'

test('Capture the creation and conversation journey for human or AI review', async ({
  page,
  evidence,
}) => {
  await captureJourney(page, evidence)
})
