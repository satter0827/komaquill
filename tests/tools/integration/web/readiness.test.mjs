import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createServer } from 'node:http'
import { mkdir, mkdtemp, writeFile, rm, utimes } from 'node:fs/promises'
import path from 'node:path'
import { waitForApi } from '../../../../tools/development/readiness.mjs'

for (const mode of ['ready', 'stale', 'foreign', 'exited', 'invalid']) {
  test(`development readiness: ${mode}`, async () => {
    const base = path.resolve('.artifacts/tmp')
    await mkdir(base, { recursive: true })
    const directory = await mkdtemp(path.join(base, 'readiness-'))
    let probes = 0
    const server = createServer((_, response) => {
      probes++
      response.setHeader('Content-Type', 'application/json')
      response.end(JSON.stringify(mode === 'invalid' ? {} : { profiles: [] }))
    })
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
    const endpoint = `http://127.0.0.1:${server.address().port}`
    const file = path.join(directory, 'listener.json')
    const started = Date.now() - 1
    await writeFile(
      file,
      JSON.stringify({
        endpoint: mode === 'foreign' ? endpoint + '/other' : endpoint,
      }),
    )
    if (mode === 'stale') await utimes(file, 0, 0)
    try {
      const ready = waitForApi(
        { exitCode: mode === 'exited' ? 1 : null },
        file,
        started,
        endpoint,
        200,
      )
      if (mode === 'ready') await ready
      else await assert.rejects(ready, /api_exited|api_startup_timeout/)
      if (['stale', 'foreign', 'exited'].includes(mode)) assert.equal(probes, 0)
    } finally {
      server.closeAllConnections()
      await new Promise((resolve) => server.close(resolve))
      assert(directory.startsWith(base + path.sep))
      await rm(directory, { recursive: true })
    }
  })
}
