import { spawnSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { readFile } from 'node:fs/promises'
import { nodeEnvironment } from '../bootstrap/node-environment.mjs'
import {
  jsonFile,
  message,
  sourceIdentity,
  verificationEnvironment,
} from './web/evidence.mjs'

const args = process.argv.slice(2)
const env = verificationEnvironment(nodeEnvironment())
let run
if (args[0] === 'test') {
  const id = new Date().toISOString().replace(/[:.]/g, '-') + '-' + randomUUID()
  const directory = path.resolve('.artifacts/records/reports/web', id)
  env.KOMAQUILL_WEB_EVIDENCE_DIR = directory
  env.KOMAQUILL_WEB_WORKSPACE = path.join(directory, 'workspace')
  env.KOMAQUILL_WEB_MODE = args.some((arg) =>
    arg.includes('scripts/web/review.config'),
  )
    ? 'exploration'
    : 'assertion'
  run = {
    format_version: 1,
    id,
    mode: env.KOMAQUILL_WEB_MODE,
    execution: args.includes('--list') ? 'discovery' : 'browser',
    started_at: new Date().toISOString(),
    source: sourceIdentity(process.cwd()),
    environment: { platform: process.platform, node: process.version },
    boundaries: {
      workspace: 'isolated',
      llm: 'loopback-scripted',
      images: 'loopback-comfy-stub',
      real_llm: false,
      gpu: false,
      image_quality: 'not_evaluated',
    },
    status: 'running',
  }
  await jsonFile(path.join(directory, 'run.json'), run)
  await jsonFile(
    path.join(directory, 'review-questions.json'),
    JSON.parse(
      await readFile(
        new URL('./web/review-questions.json', import.meta.url),
        'utf8',
      ),
    ),
  )
}
const result = spawnSync(
  process.execPath,
  ['node_modules/@playwright/test/cli.js', ...args],
  {
    stdio: 'inherit',
    env,
  },
)
let exitCode = result.status ?? 1
if (run) {
  run.finished_at = new Date().toISOString()
  run.exit_code = exitCode
  run.inputs_unchanged =
    sourceIdentity(process.cwd()).input_sha256 === run.source.input_sha256
  run.status = !run.inputs_unchanged
    ? 'inputs_changed'
    : exitCode === 0
      ? run.execution === 'discovery'
        ? 'listed'
        : 'completed'
      : result.signal
        ? 'interrupted'
        : 'failed'
  await jsonFile(path.join(env.KOMAQUILL_WEB_EVIDENCE_DIR, 'run.json'), run)
  console.log(
    message('web.evidence_saved', { path: env.KOMAQUILL_WEB_EVIDENCE_DIR }),
  )
  if (!run.inputs_unchanged) exitCode = 1
}
process.exit(exitCode)
