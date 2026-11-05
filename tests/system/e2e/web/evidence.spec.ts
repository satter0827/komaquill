import { test, expect } from './fixtures'
import { captureJourney } from '../../../../tools/testing/web/journey.mjs'
import { verifyBundle } from '../../../../tools/testing/web/evidence.mjs'

test('review collector preserves related screenshots, semantics and file hashes', async ({
  page,
  evidence,
}) => {
  await captureJourney(page, evidence, { live: true })
  const bundle = await verifyBundle(evidence.directory)
  expect(bundle).toEqual({ valid: true, problems: [] })
  expect(evidence.manifest.steps).toHaveLength(9)
  const images = evidence.manifest.files.filter(
    (file: { media_type: string }) => file.media_type === 'image/png',
  )
  expect(images).toHaveLength(9)
})
