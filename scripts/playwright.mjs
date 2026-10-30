import { spawnSync } from 'node:child_process'
import { nodeEnvironment } from '../bootstrap/node-environment.mjs'
const result = spawnSync(
  process.execPath,
  ['node_modules/@playwright/test/cli.js', ...process.argv.slice(2)],
  {
    stdio: 'inherit',
    env: nodeEnvironment(),
  },
)
process.exit(result.status ?? 1)
