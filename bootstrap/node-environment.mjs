import path from 'node:path'
import { mkdirSync } from 'node:fs'

export function nodeEnvironment(root = process.cwd()) {
  const artifact = (value) => path.resolve(root, '.artifacts', value)
  for (const directory of [
    'tmp',
    'cache/nx/workspace',
    'cache/node',
    'runtime/node',
  ])
    mkdirSync(artifact(directory), { recursive: true })
  return {
    ...process.env,
    NX_DAEMON: 'false',
    NX_ISOLATE_PLUGINS: 'false',
    NX_WORKSPACE_DATA_DIRECTORY: artifact('cache/nx/workspace'),
    NX_NATIVE_FILE_CACHE_DIRECTORY: artifact('cache/nx/native'),
    NODE_COMPILE_CACHE: artifact('cache/node'),
    PLAYWRIGHT_BROWSERS_PATH: artifact('runtime/playwright'),
    TEMP: artifact('tmp'),
    TMP: artifact('tmp'),
  }
}
