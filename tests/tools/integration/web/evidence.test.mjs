import { test } from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter, once } from 'node:events'
import { spawn } from 'node:child_process'
import { mkdtemp, readFile, rename, writeFile, rm } from 'node:fs/promises'
import { setTimeout } from 'node:timers/promises'
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
} from '../../../../tools/testing/web/evidence.mjs'
import Reporter from '../../../../tools/testing/web/reporter.mjs'
import { webConfig } from '../../../../tools/testing/web/config.mjs'

async function directory(context) {
  const root = await mkdtemp(path.resolve('.artifacts/tmp/web-evidence-'))
  context.after(() => rm(root, { recursive: true, force: true }))
  return root
}

test(
  'atomic checkpoints survive a temporary Windows replacement lock',
  {
    skip: process.platform !== 'win32',
    timeout: 15000,
  },
  async (t) => {
    const root = await directory(t)
    const target = path.join(root, 'scenario.json')
    const probe = path.join(root, 'probe.json')
    await jsonFile(target, { state: 'previous' })
    await writeFile(probe, 'probe')
    const holder = spawn(
      'powershell.exe',
      [
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        `
    $handle = [IO.File]::Open($env:KOMAQUILL_LOCK_TARGET, 'Open', 'Read', 'ReadWrite')
    try { [Console]::WriteLine('locked'); [Console]::ReadLine() | Out-Null }
    finally { $handle.Dispose() }
  `,
      ],
      {
        env: { ...process.env, KOMAQUILL_LOCK_TARGET: target },
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
      },
    )
    t.after(() => holder.kill())
    const [ready] = await once(holder.stdout, 'data', {
      signal: AbortSignal.timeout(5000),
    })
    assert.match(String(ready), /locked/)
    await assert.rejects(rename(probe, target), (error) =>
      ['EPERM', 'EACCES', 'EBUSY'].includes(error.code),
    )
    let settled = false
    const saving = jsonFile(target, { state: 'complete' }).finally(() => {
      settled = true
    })
    await setTimeout(150)
    assert.equal(settled, false)
    assert.deepEqual(JSON.parse(await readFile(target, 'utf8')), {
      state: 'previous',
    })
    const exited = once(holder, 'exit')
    holder.stdin.end('\n')
    await exited
    await saving
    assert.deepEqual(JSON.parse(await readFile(target, 'utf8')), {
      state: 'complete',
    })
  },
)

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
  assert.equal(
    route('http://localhost/api/v1/availability'),
    '/api/v1/availability',
  )
  assert.equal(
    route('http://localhost/api/v1/conversations/from-presets'),
    '/api/v1/conversations/from-presets',
  )
  assert.equal(
    route('http://localhost/api/v1/conversations/images'),
    '/api/v1/conversations/:value',
  )
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

test('artifact names cannot escape the scenario directory', async (t) => {
  const root = await directory(t)
  assert.equal(contained(root, '001.png'), path.join(root, '001.png'))
  for (const name of ['../outside', path.resolve(root, '../outside'), '.'])
    assert.throws(() => contained(root, name))
})

test('each capture keeps images, semantics, events and integrity information together', async (t) => {
  const root = await directory(t)
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
  assert.equal(manifest.outcome, 'failed')
  assert.deepEqual(
    manifest.unexpected_events.map((event) => event.kind),
    ['console'],
  )
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

test('a failed action retains its last visible state without copying exception bodies', async (t) => {
  const root = await directory(t)
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

test('expected network faults are limited by request, status, count and lifetime', async (t) => {
  const page = new Page()
  const evidence = new Evidence(page, await directory(t), { mode: 'assertion' })
  const response = (status = 503) => ({
    request: () => ({ method: () => 'POST' }),
    url: () => 'http://127.0.0.1/api/v1/images',
    status: () => status,
  })
  assert.throws(() =>
    evidence.expectFailure({
      kind: 'response',
      route: '/*',
      method: 'POST',
      status: 503,
    }),
  )
  const stop = evidence.expectFailure({
    kind: 'response',
    route: '/api/v1/images',
    method: 'POST',
    status: 503,
  })
  page.emit('response', response())
  page.emit('console', {
    type: () => 'error',
    text: () =>
      'Failed to load resource: the server responded with a status of 503 (Service Unavailable)',
    location: () => ({ url: 'http://127.0.0.1/api/v1/images' }),
  })
  page.emit('response', response(400))
  page.emit('response', response())
  stop()
  page.emit('response', response())
  page.emit('pageerror', new TypeError('private response'))
  const unexpected = evidence.unexpectedFailures()
  assert.deepEqual(
    unexpected.map((event) => event.kind),
    ['response', 'response', 'response', 'page_error'],
  )
  assert.equal(JSON.stringify(unexpected).includes('private response'), false)
  await evidence.capture('Recovering after a declared fault')
  assert.equal((await evidence.finish('passed', 'passed')).outcome, 'failed')
})

test('optional resources preserve public paths and reject foreign origins and unrelated console errors', async (t) => {
  assert.equal(
    route('http://localhost/api/userdata/user.css'),
    '/api/userdata/user.css',
  )
  assert.equal(
    route('http://localhost/api/userdata/private-work.json'),
    '/api/userdata/:value',
  )
  const page = new Page()
  const evidence = new Evidence(page, await directory(t))
  evidence.expectFailure({
    kind: 'response',
    route: '/user.css',
    method: 'GET',
    status: 404,
    origin: 'http://localhost',
  })
  for (const host of ['localhost', 'foreign.test']) {
    page.emit('response', {
      request: () => ({ method: () => 'GET' }),
      url: () => `http://${host}/user.css`,
      status: () => 404,
    })
    page.emit('console', {
      type: () => 'error',
      text: () =>
        'Failed to load resource: the server responded with a status of 404 (Not Found)',
      location: () => ({ url: `http://${host}/user.css` }),
    })
  }
  page.emit('console', {
    type: () => 'error',
    text: () => 'A real application failure',
    location: () => ({ url: 'http://localhost/user.css' }),
  })
  assert.deepEqual(
    evidence.unexpectedFailures().map((event) => event.kind),
    ['response', 'console', 'console'],
  )
})

test('only an accepted empty response can cancel its body without losing a write', async (t) => {
  const page = new Page()
  const evidence = new Evidence(page, await directory(t))
  for (const [status, length] of [
    [200, '0'],
    [200, '12'],
    [503, '0'],
    [0, '0'],
  ]) {
    const request = {
      method: () => 'POST',
      url: () => 'http://localhost/api/settings/value',
      failure: () => ({ errorText: 'net::ERR_ABORTED' }),
      resourceType: () => 'fetch',
    }
    page.emit('request', request)
    if (status)
      page.emit('response', {
        request: () => request,
        url: request.url,
        status: () => status,
        headers: () => ({ 'content-length': length }),
      })
    page.emit('requestfailed', request)
  }
  assert.equal(
    evidence.events.filter((event) => event.kind === 'empty_response_cancelled')
      .length,
    1,
  )
  assert.equal(evidence.events[0].request_id, evidence.events[1].request_id)
  assert.equal(evidence.events[0].request_id, evidence.events[2].request_id)
  assert.equal(
    evidence
      .unexpectedFailures()
      .filter((event) => event.kind === 'request_failed').length,
    3,
  )
  assert.equal(
    evidence.unexpectedFailures().filter((event) => event.kind === 'response')
      .length,
    1,
  )
})

test('a declared connection fault permits only its matching browser resource error', async (t) => {
  const page = new Page()
  const evidence = new Evidence(page, await directory(t))
  evidence.expectFailure({
    kind: 'request_failed',
    route: '/api/v1/images',
    method: 'POST',
    maximum: 1,
  })
  const url = 'http://localhost/api/v1/images'
  page.emit('requestfailed', {
    method: () => 'POST',
    url: () => url,
    failure: () => ({ errorText: 'net::ERR_FAILED' }),
  })
  for (const text of [
    'Failed to load resource: net::ERR_FAILED',
    'Failed to load resource: net::ERR_FAILED',
    'An unrelated failure',
  ])
    page.emit('console', {
      type: () => 'error',
      text: () => text,
      location: () => ({ url }),
    })
  assert.deepEqual(
    evidence.unexpectedFailures().map((event) => event.kind),
    ['console', 'console'],
  )
})

test('capture failure is checkpointed and never counted as complete evidence', async (t) => {
  const root = await directory(t)
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

test('cancelled reads remain visible while interrupted writes and network failures fail', async (t) => {
  const page = new Page()
  const evidence = new Evidence(page, await directory(t), { mode: 'assertion' })
  const request = (method, error) => ({
    method: () => method,
    url: () => 'http://127.0.0.1/api/v1/conversations/private-id',
    failure: () => ({ errorText: error }),
  })
  page.emit('requestfailed', request('GET', 'net::ERR_ABORTED'))
  page.emit('requestfailed', request('POST', 'net::ERR_ABORTED'))
  page.emit('requestfailed', request('GET', 'net::ERR_CONNECTION_RESET'))
  assert.equal(evidence.events[0].kind, 'request_cancelled')
  assert.equal(evidence.unexpectedFailures().length, 2)
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

test('unreadable, malformed and empty manifests are incomplete evidence', async (t) => {
  const root = await directory(t)
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

test('reporter distinguishes known unmet requirements and rejects missing evidence', async (t) => {
  const root = await directory(t)
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
    status: 'failed',
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

test('reporter never treats discovery or skipped tests as executed evidence', async (t) => {
  const root = await directory(t)
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
    assert.ok(
      assertions.testDir.endsWith(path.join('tests', 'system', 'e2e', 'web')),
    )
    assert.ok(
      review.testDir.endsWith(
        path.join('scripts', 'review', 'web', 'scenarios'),
      ),
    )
    assert.equal(review.webServer, undefined)
    assert.equal(review.use.baseURL, undefined)
    assert.equal(review.use.trace, 'on')
    assert.equal(review.retries, 0)
  } finally {
    if (previous === undefined) delete process.env.KOMAQUILL_WEB_EVIDENCE_DIR
    else process.env.KOMAQUILL_WEB_EVIDENCE_DIR = previous
  }
})
