import { spawnSync } from 'node:child_process'
import { nodeEnvironment } from '../../bootstrap/node-environment.mjs'
const env = nodeEnvironment()
const args = process.argv.slice(2)
if (
  ['run', 'run-many', 'affected'].includes(args[0]) &&
  !args.some((arg) => arg.startsWith('--output-style'))
)
  args.push('--output-style=static')
const result = spawnSync(
  process.execPath,
  ['node_modules/nx/dist/bin/nx.js', ...args],
  { stdio: 'inherit', env },
)
process.exit(result.status ?? 1)
