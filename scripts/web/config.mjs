import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { message } from './evidence.mjs'

export const root = fileURLToPath(new URL('../../', import.meta.url))

export function webConfig(mode = 'assertion') {
  const output = process.env.KOMAQUILL_WEB_EVIDENCE_DIR
  if (!output) throw new Error(message('web.use_runner'))
  return {
    testDir: path.join(
      root,
      mode === 'exploration' ? 'scripts/web/scenarios' : 'tests/e2e/web',
    ),
    outputDir: path.join(output, 'playwright'),
    workers: 1,
    retries: 0,
    reporter: [
      ['list'],
      [path.join(root, 'scripts/web/reporter.mjs')],
      ['html', { outputFolder: path.join(output, 'html'), open: 'never' }],
    ],
    use: {
      baseURL: 'http://127.0.0.1:8766',
      viewport: { width: 1440, height: 960 },
      screenshot: 'on',
      trace: 'on',
    },
    webServer: {
      command: 'node scripts/python.mjs -m tests.support.web_server',
      url: 'http://127.0.0.1:8766',
      cwd: root,
      reuseExistingServer: false,
      timeout: 30000,
    },
  }
}
