import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { message } from './evidence.mjs'

export const root = fileURLToPath(new URL('../../../', import.meta.url))

export function webConfig(mode = 'assertion') {
  const output = process.env.KOMAQUILL_WEB_EVIDENCE_DIR
  if (!output) throw new Error(message('web.use_runner'))
  return {
    testDir: path.join(
      root,
      mode === 'exploration'
        ? 'scripts/review/web/scenarios'
        : 'tests/system/e2e/web',
    ),
    outputDir: path.join(output, 'playwright'),
    workers: 1,
    retries: 0,
    timeout: 90000,
    expect: { timeout: 15000 },
    globalTimeout: 18 * 60 * 1000,
    reporter: [
      ['list'],
      [path.join(root, 'tools/testing/web/reporter.mjs')],
      ['html', { outputFolder: path.join(output, 'html'), open: 'never' }],
    ],
    use: {
      viewport: { width: 1440, height: 960 },
      screenshot: 'on',
      trace: 'on',
    },
  }
}
