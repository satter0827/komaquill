import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'

const python = process.env.KOMAQUILL_PYTHON ?? path.resolve('.artifacts/runtime/venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
if (!existsSync(python)) throw new Error('Run bootstrap/setup.ps1 to prepare the Python environment.')
const children = [
  spawn(python, ['-B', '-m', 'komaquill', '--project', process.cwd(), 'web', '--port', '8765'], { stdio: 'inherit' }),
  spawn(process.execPath, ['../../node_modules/vite/bin/vite.js', '--host', '127.0.0.1'], { cwd: 'src/frontend', stdio: 'inherit' }),
]
let closing = false
function close(code = 0) { if (closing) return; closing = true; for (const child of children) child.kill(); process.exitCode = code }
for (const child of children) { child.on('error', () => close(1)); child.on('exit', code => close(code ?? 1)) }
process.on('SIGINT', () => close())
process.on('SIGTERM', () => close())
