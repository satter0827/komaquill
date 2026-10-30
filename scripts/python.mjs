import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
const python = process.env.KOMAQUILL_PYTHON ?? path.resolve('.artifacts/runtime/venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const localUv = path.resolve('.artifacts/runtime/uv/uv.exe')
const uv = process.env.KOMAQUILL_UV ?? (process.platform === 'win32' && existsSync(localUv) ? localUv : 'uv')
const result = spawnSync(python, ['-B', ...process.argv.slice(2)], { stdio: 'inherit', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1', KOMAQUILL_UV: uv } })
if (result.error) process.stderr.write('Python is unavailable. Run bootstrap/setup.ps1.\n')
process.exit(result.status ?? 1)
