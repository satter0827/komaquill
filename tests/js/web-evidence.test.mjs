import { test } from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { mkdtemp, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import {
  Evidence,
  classify,
  contained,
  diagnostic,
  hash,
  route,
  verifyBundle,
  verificationEnvironment,
  jsonFile,
} from '../../scripts/web/evidence.mjs'
import Reporter from '../../scripts/web/reporter.mjs'
import { webConfig } from '../../scripts/web/config.mjs'

async function directory() {
  return mkdtemp(path.resolve('.artifacts/tmp/web-evidence-'))
}

class Page extends EventEmitter {
  url() {
    return 'http://127.0.0.1:8766/conversations/private-value?token=private-query'
  }
  async screenshot() {
    return Buffer.from('unit-test-screenshot-bytes')
  }
  locator() {
    return { ariaSnapshot: async () => '- heading "Fixture"' }
  }
  async evaluate() {
    return {
      language: 'ja',
      headings: [{ level: 1, text: 'Fixture' }],
      controls: [],
      images: [],
    }
  }
}

test('network routes discard credentials, query values and unknown path segments', () => {
  assert.equal(
    route(
      'http://user:private-password@example.test/api/v1/conversations/private-id/turns?key=private-query#secret', // pragma: allowlist secret - redaction fixture
    ),
    '/api/v1/conversations/:value/turns',
  )
  assert.equal(route('not a URL'), ':invalid-url')
  const value = diagnostic(new Error('private diagnostic payload'))
  assert.equal(value.type, 'Error')
  assert.equal(JSON.stringify(value).includes('private'), false)
})

test('expected failure, skip and exploration completion are distinct from passing assertions', () => {
  assert.equal(classify('failed', 'failed', 'assertion'), 'known_unmet')
  assert.equal(classify('passed', 'failed', 'assertion'), 'unexpected_pass')
  assert.equal(classify('skipped', 'passed', 'assertion'), 'not_executed')
  assert.equal(classify('interrupted', 'passed', 'assertion'), 'interrupted')
  assert.equal(classify('passed', 'passed', 'exploration'), 'captured')
  assert.equal(classify('failed', 'passed', 'exploration'), 'failed')
  assert.equal(classify('timedOut', 'failed', 'assertion'), 'failed')
})

test('artifact names cannot escape the scenario directory', async () => {
  const root = await directory()
  assert.equal(contained(root, '001.png'), path.join(root, '001.png'))
  for (const name of ['../outside', path.resolve(root, '../outside'), '.'])
    assert.throws(() => contained(root, name))
})

test('each capture keeps images, semantics, events and integrity information together', async () => {
  const root = await directory()
  const page = new Page()
  const evidence = new Evidence(page, root, { mode: 'assertion' })
  page.emit('request', {
    method: () => 'POST',
    url: () => page.url(),
    resourceType: () => 'fetch',
  })
  page.emit('response', {
    request: () => ({ method: () => 'POST' }),
    url: () => page.url(),
    status: () => 202,
  })
  page.emit('console', {
    type: () => 'error',
    text: () => 'private-console-value',
  })
  await evidence.capture('A completed UI action')
  const manifest = await evidence.finish('passed', 'passed')
  assert.equal(manifest.outcome, 'passed')
  assert.equal(manifest.quality_review, 'not_reviewed')
  assert.equal(manifest.steps.length, 1)
  assert.equal(manifest.files.length, 4)
  assert.deepEqual(await verifyBundle(root), { valid: true, problems: [] })
  const events = await readFile(path.join(root, 'events.jsonl'), 'utf8')
  assert.equal(events.includes('private'), false)
  assert.equal(page.listenerCount('request'), 0)
  assert.equal(
    manifest.files[0].sha256,
    hash(Buffer.from('unit-test-screenshot-bytes')),
  )
  await writeFile(path.join(root, '001.png'), 'changed')
  assert.deepEqual((await verifyBundle(root)).problems, [
    { path: '001.png', code: 'content_mismatch' },
  ])
})

test('a failed action retains its last visible state without copying exception bodies', async () => {
  const root = await directory()
  const evidence = new Evidence(new Page(), root, { mode: 'exploration' })
  await assert.rejects(
    evidence.step('Failed action', async () => {
      throw new Error('private original error')
    }),
  )
  await evidence.finish('failed', 'passed')
  assert.equal((await verifyBundle(root)).valid, true)
  assert.equal(
    (await readFile(path.join(root, 'scenario.json'), 'utf8')).includes(
      'private original error',
    ),
    false,
  )
})

test('capture failure is checkpointed and never counted as complete evidence', async () => {
  const root = await directory()
  const page = new Page()
  page.screenshot = async () => {
    throw new Error('private capture failure')
  }
  const evidence = new Evidence(page, root, { mode: 'exploration' })
  await assert.rejects(evidence.capture('Unavailable screenshot'))
  await evidence.finish('failed', 'passed')
  const result = await verifyBundle(root)
  assert.equal(result.valid, false)
  assert.equal(result.problems.length, 3)
})

test('verification discards inherited provider credentials and live permissions', () => {
  const input = {
    PATH: 'fixture-path',
    KOMAQUILL_PYTHON: 'fixture-python',
    KOMAQUILL_ALLOW_REAL_LLM: '1',
    KOMAQUILL_REAL_LLM: '1',
    komaquill_live_test: '1',
    KOMAQUILL_WEB_WORKSPACE: 'inherited-workspace',
    LANGSMITH_API_KEY: 'private', // pragma: allowlist secret - inherited environment fixture
    LANGCHAIN_TRACING_V2: 'true',
    OPENAI_API_KEY: 'private', // pragma: allowlist secret - inherited environment fixture
    OPENAI_BASE_URL: 'https://example.test',
  }
  assert.deepEqual(verificationEnvironment(input), {
    PATH: 'fixture-path',
    KOMAQUILL_PYTHON: 'fixture-python',
  })
  assert.equal(input.KOMAQUILL_REAL_LLM, '1')
})

test('unreadable, malformed and empty manifests are incomplete evidence', async () => {
  const root = await directory()
  assert.equal((await verifyBundle(root)).valid, false)
  await writeFile(path.join(root, 'scenario.json'), 'broken')
  assert.equal((await verifyBundle(root)).valid, false)
  await jsonFile(path.join(root, 'scenario.json'), { format_version: 1 })
  assert.equal((await verifyBundle(root)).valid, false)
  await jsonFile(path.join(root, 'scenario.json'), {
    format_version: 1,
    files: [],
    steps: [],
  })
  assert.deepEqual((await verifyBundle(root)).problems, [{ code: 'no_steps' }])
})

test('reporter distinguishes known unmet requirements and rejects missing evidence', async () => {
  const root = await directory()
  await jsonFile(path.join(root, 'run.json'), {
    id: 'fixture',
    execution: 'browser',
  })
  const env = {
    KOMAQUILL_WEB_EVIDENCE_DIR: root,
    KOMAQUILL_WEB_MODE: 'assertion',
  }
  const reporter = new Reporter({}, env)
  const id = 'known-requirement'
  const evidence = new Evidence(
    new Page(),
    path.join(root, 'scenarios', hash(id).slice(0, 20) + '-0'),
  )
  await evidence.capture('Unmet requirement')
  await evidence.finish('failed', 'failed')
  reporter.onTestEnd(
    { id, title: id, expectedStatus: 'failed' },
    { status: 'failed', retry: 0 },
  )
  assert.deepEqual(await reporter.onEnd({ status: 'passed' }), {
    status: 'passed',
  })
  let index = JSON.parse(await readFile(path.join(root, 'index.json'), 'utf8'))
  assert.deepEqual(index.counts, { known_unmet: 1 })
  assert.equal(index.evidence_complete, true)
  reporter.onTestEnd(
    { id: 'missing', title: 'missing', expectedStatus: 'passed' },
    { status: 'passed', retry: 0 },
  )
  assert.deepEqual(await reporter.onEnd({ status: 'passed' }), {
    status: 'failed',
  })
  index = JSON.parse(await readFile(path.join(root, 'index.json'), 'utf8'))
  assert.equal(index.evidence_complete, false)
})

test('reporter never treats discovery or skipped tests as executed evidence', async () => {
  const root = await directory()
  const env = { KOMAQUILL_WEB_EVIDENCE_DIR: root }
  const reporter = new Reporter({}, env)
  await jsonFile(path.join(root, 'run.json'), {
    id: 'fixture',
    execution: 'discovery',
  })
  assert.deepEqual(await reporter.onEnd({ status: 'passed' }), {
    status: 'passed',
  })
  const index = JSON.parse(
    await readFile(path.join(root, 'index.json'), 'utf8'),
  )
  assert.equal(index.evidence_complete, null)
  await jsonFile(path.join(root, 'run.json'), {
    id: 'fixture',
    execution: 'browser',
  })
  reporter.onTestEnd(
    { id: 'skipped', title: 'skipped', expectedStatus: 'skipped' },
    { status: 'skipped', retry: 0 },
  )
  assert.deepEqual(await reporter.onEnd({ status: 'passed' }), {
    status: 'failed',
  })
})

test('browser configurations isolate assertions and review collection in fresh output', () => {
  const previous = process.env.KOMAQUILL_WEB_EVIDENCE_DIR
  try {
    delete process.env.KOMAQUILL_WEB_EVIDENCE_DIR
    assert.throws(() => webConfig())
    process.env.KOMAQUILL_WEB_EVIDENCE_DIR = path.resolve(
      '.artifacts/tmp/config-fixture',
    )
    const assertions = webConfig()
    const review = webConfig('exploration')
    assert.ok(assertions.testDir.endsWith(path.join('tests', 'e2e', 'web')))
    assert.ok(review.testDir.endsWith(path.join('scripts', 'web', 'scenarios')))
    assert.equal(review.webServer.reuseExistingServer, false)
    assert.equal(review.use.trace, 'on')
    assert.equal(review.retries, 0)
  } finally {
    if (previous === undefined) delete process.env.KOMAQUILL_WEB_EVIDENCE_DIR
    else process.env.KOMAQUILL_WEB_EVIDENCE_DIR = previous
  }
})
