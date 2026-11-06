import { readFile, stat } from 'node:fs/promises'
import { setTimeout as delay } from 'node:timers/promises'

export const STARTUP_TIMEOUT_MS = 30_000
const PROBE_TIMEOUT_MS = 1000
const PROBE_INTERVAL_MS = 50

export async function waitForApi(
  child,
  file,
  started,
  endpoint,
  timeout = STARTUP_TIMEOUT_MS,
) {
  const deadline = performance.now() + timeout
  while (performance.now() < deadline) {
    if (child.exitCode !== null) throw new Error('api_exited')
    try {
      const info = await stat(file)
      const listener = JSON.parse(await readFile(file, 'utf8'))
      if (info.mtimeMs >= started && listener.endpoint === endpoint) {
        const response = await fetch(endpoint + '/api/v1/settings', {
          signal: AbortSignal.timeout(
            Math.min(
              PROBE_TIMEOUT_MS,
              Math.max(1, Math.ceil(deadline - performance.now())),
            ),
          ),
        })
        const body = response.ok ? await response.json() : null
        if (Array.isArray(body?.profiles) && child.exitCode === null) return
      }
    } catch {
      // Keep waiting for this launch's listener and a usable API response.
    }
    await delay(
      Math.min(PROBE_INTERVAL_MS, Math.max(0, deadline - performance.now())),
    )
  }
  throw new Error('api_startup_timeout')
}
