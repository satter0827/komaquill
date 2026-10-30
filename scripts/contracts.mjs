import { createClient } from '@hey-api/openapi-ts'
import { spawnSync } from 'node:child_process'
import { readdir, readFile, mkdir } from 'node:fs/promises'
import path from 'node:path'
import { randomUUID } from 'node:crypto'

const write = process.argv.includes('--write')
const result = spawnSync(
  process.execPath,
  ['scripts/python.mjs', 'scripts/contracts.py', ...(write ? [] : ['--check'])],
  { stdio: 'inherit' },
)
if (result.status !== 0) process.exit(result.status ?? 1)
const destination = 'src/frontend/src/api/generated'
const output = write ? destination : `.artifacts/tmp/contracts/${randomUUID()}`
await mkdir(output, { recursive: true })
await createClient({
  configFile: false,
  input: './src/contracts/openapi.json',
  output,
  plugins: ['@hey-api/typescript', '@hey-api/sdk', '@hey-api/client-fetch'],
})
if (!write) {
  const files = await readdir(output, { recursive: true, withFileTypes: true })
  const expected = files
    .filter((file) => file.isFile())
    .map((file) => path.relative(output, path.join(file.parentPath, file.name)))
    .sort()
  const actualFiles = (
    await readdir(destination, { recursive: true, withFileTypes: true })
  )
    .filter((file) => file.isFile())
    .map((file) =>
      path.relative(destination, path.join(file.parentPath, file.name)),
    )
    .sort()
  if (JSON.stringify(expected) !== JSON.stringify(actualFiles))
    throw new Error('Regenerate the API client: generated file list differs.')
  for (const file of files.filter((file) => file.isFile())) {
    const source = path.join(file.parentPath, file.name)
    const relative = path.relative(output, source)
    const actual = await readFile(path.join(destination, relative))
    if (!actual.equals(await readFile(source)))
      throw new Error(`Regenerate the API client: ${relative}`)
  }
}
