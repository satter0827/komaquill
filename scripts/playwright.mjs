import { spawnSync } from 'node:child_process'
import path from 'node:path'
const result = spawnSync(process.execPath, ['node_modules/@playwright/test/cli.js', ...process.argv.slice(2)], {
  stdio: 'inherit',
  env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: path.resolve('.artifacts/runtime/playwright') },
})
process.exit(result.status ?? 1)
