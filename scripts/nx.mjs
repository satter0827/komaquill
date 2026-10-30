import { spawnSync } from 'node:child_process'
import path from 'node:path'
const env = {
  ...process.env,
  NX_DAEMON: 'false',
  NX_ISOLATE_PLUGINS: 'false',
  NX_WORKSPACE_DATA_DIRECTORY: path.resolve('.artifacts/cache/nx/workspace'),
  TEMP: path.resolve('.artifacts/tmp'),
  TMP: path.resolve('.artifacts/tmp'),
}
const result = spawnSync(process.execPath, ['node_modules/nx/dist/bin/nx.js', ...process.argv.slice(2)], { stdio: 'inherit', env })
process.exit(result.status ?? 1)
