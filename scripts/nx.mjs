import { spawnSync } from 'node:child_process'
import { nodeEnvironment } from '../bootstrap/node-environment.mjs'
const env = nodeEnvironment()
const result = spawnSync(
  process.execPath,
  ['node_modules/nx/dist/bin/nx.js', ...process.argv.slice(2)],
  { stdio: 'inherit', env },
)
process.exit(result.status ?? 1)
