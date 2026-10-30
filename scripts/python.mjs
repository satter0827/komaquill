import { spawnSync } from 'node:child_process'
import path from 'node:path'
const python = process.env.KOMAQUILL_PYTHON ?? path.resolve('.artifacts/runtime/venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const result = spawnSync(python, ['-B', ...process.argv.slice(2)], { stdio: 'inherit', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1', KOMAQUILL_UV: process.env.KOMAQUILL_UV ?? (process.platform === 'win32' ? path.resolve('.artifacts/runtime/uv/uv.exe') : 'uv') } })
if (result.error) process.stderr.write('Python is unavailable. Run bootstrap/setup.ps1.\n')
process.exit(result.status ?? 1)
