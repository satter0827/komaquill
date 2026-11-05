import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, existsSync } from 'node:fs'
import path from 'node:path'
import { nodeEnvironment } from '../../../../bootstrap/node-environment.mjs'

test('Node tools share contained artifact directories', () => {
  const root = mkdtempSync(path.resolve('.artifacts/tmp/node-environment-'))
  const before = process.env.NX_WORKSPACE_DATA_DIRECTORY
  const env = nodeEnvironment(root)
  for (const key of [
    'NX_WORKSPACE_DATA_DIRECTORY',
    'NX_NATIVE_FILE_CACHE_DIRECTORY',
    'NODE_COMPILE_CACHE',
    'PLAYWRIGHT_BROWSERS_PATH',
    'TEMP',
    'TMP',
  ]) {
    assert.ok(env[key].startsWith(path.join(root, '.artifacts') + path.sep))
  }
  assert.ok(existsSync(env.TEMP))
  assert.equal(env.NX_DAEMON, 'false')
  assert.equal(process.env.NX_WORKSPACE_DATA_DIRECTORY, before)
})
