import { spawn } from 'node:child_process'
import { createWriteStream } from 'node:fs'
import { mkdir, readFile } from 'node:fs/promises'
import path from 'node:path'
import type { TestInfo } from '@playwright/test'

export async function isolatedServer(
  use: (url: string) => Promise<void>,
  info: TestInfo,
) {
  const directory = info.outputPath('server')
  const workspace = path.join(directory, 'workspace')
  await mkdir(directory, { recursive: true })
  const output = createWriteStream(path.join(directory, 'server.log'))
  const child = spawn(
    process.env.KOMAQUILL_WEB_PYTHON!,
    ['-B', '-m', 'tests.system.support.web_server'],
    {
      env: {
        ...process.env,
        KOMAQUILL_WEB_WORKSPACE: workspace,
        KOMAQUILL_WEB_CASE_DIR: directory,
        PYTHONUNBUFFERED: '1',
      },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    },
  )
  child.stdout.pipe(output, { end: false })
  child.stderr.pipe(output, { end: false })
  const exited = new Promise<void>((resolve) =>
    child.once('close', () => resolve()),
  )
  let startupError: Error | undefined
  child.on('error', (error) => {
    startupError = error
  })
  try {
    let endpoint: string | undefined
    const deadline = Date.now() + 30000
    while (Date.now() < deadline) {
      if (startupError) throw startupError
      if (child.exitCode !== null)
        throw new Error('The isolated application stopped during startup.')
      try {
        const listener = JSON.parse(
          await readFile(
            path.join(workspace, '.artifacts/state/web-listener.json'),
            'utf8',
          ),
        )
        const result = await fetch(listener.endpoint + '/api/v1/settings', {
          signal: AbortSignal.timeout(1000),
        })
        if (result.ok) {
          endpoint = listener.endpoint
          break
        }
      } catch {
        /* Read only this process's endpoint until it becomes ready. */
      }
      await new Promise((resolve) => setTimeout(resolve, 50))
    }
    if (!endpoint)
      throw new Error(
        'The isolated application did not become ready within the startup limit.',
      )
    await use(endpoint)
  } finally {
    child.stdin.end('\n')
    const stopped = await Promise.race([
      exited.then(() => true),
      new Promise<boolean>((resolve) =>
        setTimeout(() => resolve(false), 15000),
      ),
    ])
    if (!stopped) {
      child.kill()
      throw new Error('The isolated application did not confirm shutdown.')
    }
    output.end()
  }
}
