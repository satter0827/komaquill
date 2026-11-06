import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { nodeEnvironment } from '../../bootstrap/node-environment.mjs'
import { waitForApi } from './readiness.mjs'
import { message } from '../testing/web/evidence.mjs'

const python =
  process.env.KOMAQUILL_PYTHON ??
  path.resolve(
    '.artifacts/runtime/venv',
    process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
  )
if (!existsSync(python))
  throw new Error('Run bootstrap/setup.ps1 to prepare the Python environment.')
const children = []
const SHUTDOWN_TIMEOUT_MS = 5000
let closing = false
function close(code = 0) {
  if (closing) return
  closing = true
  for (const child of children) child.kill()
  setTimeout(() => {
    for (const child of children)
      if (child.exitCode === null) child.kill('SIGKILL')
  }, SHUTDOWN_TIMEOUT_MS).unref()
  process.exitCode = code
}
function launch(command, args, cwd = process.cwd()) {
  const child = spawn(command, args, {
    cwd,
    stdio: 'inherit',
    env: nodeEnvironment(),
  })
  children.push(child)
  child.on('error', () => close(1))
  child.on('exit', (code) => close(code ?? 1))
  return child
}
process.on('SIGINT', () => close())
process.on('SIGTERM', () => close())
const started = Date.now()
const api = launch(python, [
  '-B',
  '-m',
  'komaquill',
  '--project',
  process.cwd(),
  'web',
  '--port',
  '8765',
])
try {
  await waitForApi(
    api,
    path.resolve('.artifacts/state/web-listener.json'),
    started,
    'http://127.0.0.1:8765',
  )
  if (!closing)
    launch(
      process.execPath,
      ['node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--strictPort'],
      'src/frontend',
    )
} catch {
  if (!closing) console.error(message('development.startup_failed'))
  close(1)
}
