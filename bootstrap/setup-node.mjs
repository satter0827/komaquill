import { spawnSync } from 'node:child_process'
import { nodeEnvironment } from './node-environment.mjs'

const result = spawnSync(
  process.platform === 'win32' ? 'pnpm.cmd' : 'pnpm',
  ['install', '--frozen-lockfile', ...process.argv.slice(2)],
  {
    env: nodeEnvironment(),
    stdio: 'inherit',
    shell: process.platform === 'win32',
  },
)
process.exit(result.status ?? 1)
