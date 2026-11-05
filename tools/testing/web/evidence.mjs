import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { writeFile as atomicWriteFile } from 'atomically'

export const hash = (value) => createHash('sha256').update(value).digest('hex')
const time = () => new Date().toISOString()
const RESOURCE_CONSOLE_EVENT_WINDOW = 3
const networkFailureCode = (text) => {
  const code = /^net::(ERR_[A-Z_]+)$/.exec(text ?? '')?.[1]
  return new Set([
    'ERR_ABORTED',
    'ERR_FAILED',
    'ERR_CONNECTION_REFUSED',
    'ERR_CONNECTION_RESET',
    'ERR_TIMED_OUT',
    'ERR_EMPTY_RESPONSE',
    'ERR_INTERNET_DISCONNECTED',
    'ERR_NAME_NOT_RESOLVED',
  ]).has(code)
    ? code
    : 'network_error'
}
const messages = JSON.parse(
  readFileSync(
    new URL('../../../scripts/resources/messages.json', import.meta.url),
    'utf8',
  ),
)
export function message(key, values = {}) {
  return messages[key].replace(/\{([a-z_]+)\}/g, (_, name) =>
    String(values[name]),
  )
}

export function diagnostic(error) {
  return {
    type: error?.constructor?.name ?? 'UnknownError',
    fingerprint: hash(String(error?.message ?? error)),
  }
}

export function route(url) {
  try {
    const parsed = new URL(url)
    const publicFiles = new Set([
      '/user.css',
      '/api/userdata/user.css',
      '/api/userdata/comfy.templates.json',
    ])
    if (publicFiles.has(parsed.pathname)) return parsed.pathname
    const segments = parsed.pathname.split('/').filter(Boolean)
    const fixed = new Set([
      'api',
      'v1',
      'players',
      'characters',
      'stories',
      'conversations',
      'from-presets',
      'turns',
      'operations',
      'commands',
      'preferences',
      'settings',
      'runtime',
      'refresh',
      'views',
      'images',
      'files',
      'collect',
      'creation-jobs',
      'archive',
      'duplicate',
      'start',
      'stop',
      'new',
      'assets',
      'userdata',
      'global_subgraphs',
      'experiment',
      'models',
      'object_info',
      'queue',
      'history',
      'prompt',
      'extensions',
      'features',
      'node_replacements',
      'system_stats',
      'users',
    ])
    return (
      '/' +
      segments
        .map((segment) => (fixed.has(segment) ? segment : ':value'))
        .join('/')
    )
  } catch {
    return ':invalid-url'
  }
}

export function contained(root, name) {
  const result = path.resolve(root, name)
  const relative = path.relative(path.resolve(root), result)
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative))
    throw new Error(message('web.invalid_evidence_path'))
  return result
}

export async function jsonFile(file, value) {
  await atomicWriteFile(file, JSON.stringify(value, null, 2) + '\n')
}

export function sourceIdentity(root) {
  const git = (...args) =>
    execFileSync('git', args, {
      cwd: root,
      encoding: 'utf8',
      windowsHide: true,
    }).trim()
  const digest = execFileSync(
    process.execPath,
    [
      'tools/runtime/python.mjs',
      '-c',
      'from pathlib import Path; from tools.reporting import input_fingerprint; print(input_fingerprint(Path.cwd()))',
    ],
    { cwd: root, encoding: 'utf8', windowsHide: true },
  ).trim()
  return {
    commit: git('rev-parse', 'HEAD'),
    input_sha256: digest,
    dirty: !!git('status', '--porcelain'),
  }
}

export function classify(status, expected, mode) {
  if (status === 'skipped') return 'not_executed'
  if (status === 'interrupted') return 'interrupted'
  if (status === 'timedOut') return 'failed'
  if (expected === 'failed')
    return status === 'failed' ? 'known_unmet' : 'unexpected_pass'
  return status === 'passed'
    ? mode === 'exploration'
      ? 'captured'
      : 'passed'
    : 'failed'
}

export function verificationEnvironment(environment) {
  return Object.fromEntries(
    Object.entries(environment).filter(([name]) => {
      const key = name.toUpperCase()
      return !(
        (key.startsWith('KOMAQUILL_') && key !== 'KOMAQUILL_PYTHON') ||
        /^(LANGCHAIN_|LANGSMITH_|OPENAI_)/.test(key)
      )
    }),
  )
}

export class Evidence {
  constructor(page, directory, metadata = {}) {
    this.page = page
    this.directory = directory
    this.manifest = {
      format_version: 1,
      ...metadata,
      started_at: time(),
      status: 'running',
      quality_review: 'not_reviewed',
      steps: [],
      files: [],
    }
    this.events = []
    this.expectedFailures = []
    this.listeners = []
    const requests = new WeakMap()
    let requestId = 0
    const identity = (request) => {
      if (!requests.has(request))
        requests.set(request, { request_id: ++requestId })
      return requests.get(request)
    }
    const origin = (url) => {
      try {
        return hash(new URL(url).origin)
      } catch {
        return null
      }
    }
    const add = (kind, payload) =>
      this.events.push({
        sequence: this.events.length + 1,
        observed_at: time(),
        kind,
        ...payload,
      })
    this.listen('request', (request) =>
      add('request', {
        method: request.method(),
        route: route(request.url()),
        resource_type: request.resourceType(),
        ...identity(request),
        origin_sha256: origin(request.url()),
      }),
    )
    this.listen('response', (response) => {
      const request = response.request()
      const state = identity(request)
      state.response_status = response.status()
      state.empty_response = response.headers?.()['content-length'] === '0'
      add('response', {
        method: request.method(),
        route: route(response.url()),
        status: response.status(),
        request_id: state.request_id,
        origin_sha256: origin(response.url()),
      })
    })
    this.listen('requestfailed', (request) => {
      const state = identity(request)
      const cancelled = request.failure?.()?.errorText === 'net::ERR_ABORTED'
      const emptySuccess =
        state.empty_response &&
        state.response_status >= 200 &&
        state.response_status < 300
      add(
        cancelled && emptySuccess
          ? 'empty_response_cancelled'
          : request.method() === 'GET' && cancelled
            ? 'request_cancelled'
            : 'request_failed',
        {
          method: request.method(),
          route: route(request.url()),
          ...state,
          origin_sha256: origin(request.url()),
          failure_code: networkFailureCode(request.failure?.()?.errorText),
        },
      )
    })
    this.listen('console', (message) =>
      add('console', {
        level: message.type(),
        route: route(message.location?.().url ?? ''),
        fingerprint: hash(message.text()),
        origin_sha256: origin(message.location?.().url ?? ''),
        resource_status:
          Number(
            /^Failed to load resource: the server responded with a status of (\d{3})\b/.exec(
              message.text(),
            )?.[1],
          ) || null,
        resource_failure_code: message
          .text()
          .startsWith('Failed to load resource: net::')
          ? networkFailureCode(
              message.text().slice('Failed to load resource: '.length),
            )
          : null,
      }),
    )
    this.listen('pageerror', (error) => add('page_error', diagnostic(error)))
  }

  /** @param {{kind: 'response'|'request_failed', route: string, method: string, status?: number, maximum?: number, origin?: string}} expectation */
  expectFailure({
    kind,
    route: expectedRoute,
    method,
    status,
    maximum = 1,
    origin,
  }) {
    if (
      !['response', 'request_failed'].includes(kind) ||
      !expectedRoute?.startsWith('/') ||
      expectedRoute.includes('*') ||
      !method ||
      (kind === 'response' &&
        (!Number.isInteger(status) || status < 400 || status > 599)) ||
      !Number.isSafeInteger(maximum) ||
      maximum < 1
    )
      throw new Error(message('web.invalid_failure_expectation'))
    const expected = {
      kind,
      route: expectedRoute,
      method,
      status,
      maximum,
      origin_sha256: origin ? hash(new URL(origin).origin) : undefined,
      after: this.events.length,
      until: Infinity,
    }
    this.expectedFailures.push(expected)
    return () => {
      expected.until = this.events.length
    }
  }

  unexpectedFailures() {
    const consumed = new Map()
    const expectedEvents = new Set()
    for (const event of this.events) {
      const expected = this.expectedFailures.find(
        (rule) =>
          event.sequence > rule.after &&
          event.sequence <= rule.until &&
          event.kind === rule.kind &&
          event.route === rule.route &&
          event.method === rule.method &&
          (rule.origin_sha256 === undefined ||
            event.origin_sha256 === rule.origin_sha256) &&
          (rule.status === undefined || event.status === rule.status) &&
          (consumed.get(rule) ?? 0) < rule.maximum,
      )
      if (expected) {
        consumed.set(expected, (consumed.get(expected) ?? 0) + 1)
        expectedEvents.add(event)
      }
    }
    const correlatedConsole = new Set()
    return this.events.filter((event) => {
      const failure =
        event.kind === 'page_error' ||
        event.kind === 'request_failed' ||
        (event.kind === 'response' && event.status >= 400) ||
        (event.kind === 'console' && event.level === 'error')
      if (!failure || expectedEvents.has(event)) return false
      if (
        event.kind === 'console' &&
        [...expectedEvents].some((network) => {
          if (
            correlatedConsole.has(network) ||
            network.route !== event.route ||
            network.origin_sha256 !== event.origin_sha256 ||
            !(
              (network.kind === 'response' &&
                network.status === event.resource_status) ||
              (network.kind === 'request_failed' &&
                network.failure_code !== 'network_error' &&
                network.failure_code === event.resource_failure_code)
            ) ||
            Math.abs(network.sequence - event.sequence) >
              RESOURCE_CONSOLE_EVENT_WINDOW
          )
            return false
          correlatedConsole.add(network)
          return true
        })
      )
        return false
      return true
    })
  }

  listen(name, callback) {
    this.page.on(name, callback)
    this.listeners.push([name, callback])
  }

  async file(name, content, mediaType) {
    const target = contained(this.directory, name)
    await mkdir(path.dirname(target), { recursive: true })
    await writeFile(target, content, { flag: 'wx' })
    this.manifest.files.push({
      path: name,
      media_type: mediaType,
      bytes: Buffer.byteLength(content),
      sha256: hash(content),
    })
    return name
  }

  async capture(label) {
    const id = String(this.manifest.steps.length + 1).padStart(3, '0')
    const step = {
      id,
      label,
      observed_at: time(),
      event_sequence: this.events.length,
      route: route(this.page.url()),
      artifacts: {},
    }
    try {
      // Browser screenshots and semantic snapshots are evidence, not quality verdicts.
      step.artifacts.screenshot = await this.file(
        `${id}.png`,
        await this.page.screenshot({
          type: 'png',
          fullPage: true,
          animations: 'disabled',
        }),
        'image/png',
      )
      step.artifacts.accessibility = await this.file(
        `${id}.aria.txt`,
        await this.page.locator('body').ariaSnapshot(),
        'text/plain',
      )
      const dom = await this.page.evaluate(() => {
        const visible = (element) =>
          !!element.getClientRects().length &&
          getComputedStyle(element).visibility !== 'hidden'
        const label = (element) =>
          element.getAttribute('aria-label') ||
          element.labels?.[0]?.innerText ||
          element.innerText ||
          ''
        return {
          language: document.documentElement.lang,
          title: document.title,
          viewport: {
            width: innerWidth,
            height: innerHeight,
            scroll_width: document.documentElement.scrollWidth,
          },
          text: document.body.innerText,
          headings: [...document.querySelectorAll('h1,h2,h3')]
            .filter(visible)
            .map((element) => ({
              level: Number(element.tagName.slice(1)),
              text: element.innerText,
            })),
          controls: [
            ...document.querySelectorAll(
              'input,textarea,select,button,a,summary',
            ),
          ]
            .filter(visible)
            .map((element) => {
              const secret = /password|secret|token|api.?key|credential/i.test(
                [
                  element.type,
                  element.name,
                  element.id,
                  element.autocomplete,
                  label(element),
                ].join(' '),
              )
              return {
                tag: element.tagName.toLowerCase(),
                role: element.getAttribute('role'),
                label: label(element),
                disabled: !!element.disabled,
                value: secret ? '[redacted]' : (element.value ?? null),
                checked: element.checked ?? null,
              }
            }),
          images: [...document.images].filter(visible).map((element) => ({
            alt: element.alt,
            loaded: element.complete && element.naturalWidth > 0,
            width: element.naturalWidth,
            height: element.naturalHeight,
            displayed_width: element.clientWidth,
            displayed_height: element.clientHeight,
          })),
        }
      })
      step.artifacts.dom = await this.file(
        `${id}.dom.json`,
        JSON.stringify(dom, null, 2) + '\n',
        'application/json',
      )
    } catch (error) {
      step.capture_error = diagnostic(error)
      throw error
    } finally {
      this.manifest.steps.push(step)
      await this.checkpoint()
    }
    return step
  }

  async step(label, action) {
    let failure
    try {
      return await action()
    } catch (error) {
      failure = error
      throw error
    } finally {
      try {
        await this.capture(label)
      } catch (error) {
        if (!failure) throw error
      }
    }
  }

  async checkpoint() {
    await jsonFile(path.join(this.directory, 'scenario.json'), this.manifest)
  }

  async finish(status, expectedStatus) {
    for (const [name, callback] of this.listeners) this.page.off(name, callback)
    await this.file(
      'events.jsonl',
      this.events.map((event) => JSON.stringify(event)).join('\n') + '\n',
      'application/x-ndjson',
    )
    const result = Object.assign(this.manifest, {
      status,
      expected_status: expectedStatus,
      unexpected_events: this.unexpectedFailures(),
      expected_failures: this.expectedFailures.map((rule) => ({
        ...rule,
        until: Number.isFinite(rule.until) ? rule.until : this.events.length,
      })),
      outcome:
        this.unexpectedFailures().length && this.manifest.mode !== 'exploration'
          ? 'failed'
          : classify(status, expectedStatus, this.manifest.mode),
      finished_at: time(),
    })
    await this.checkpoint()
    return result
  }
}

export async function verifyBundle(directory) {
  let manifest
  try {
    manifest = JSON.parse(
      await readFile(path.join(directory, 'scenario.json'), 'utf8'),
    )
  } catch {
    return { valid: false, problems: [{ code: 'manifest_unreadable' }] }
  }
  const problems = []
  if (
    manifest?.format_version !== 1 ||
    !Array.isArray(manifest.files) ||
    !Array.isArray(manifest.steps)
  )
    return { valid: false, problems: [{ code: 'manifest_invalid' }] }
  if (!manifest.steps.length) problems.push({ code: 'no_steps' })
  for (const file of manifest.files) {
    try {
      const data = await readFile(contained(directory, file.path))
      if (hash(data) !== file.sha256 || data.length !== file.bytes)
        problems.push({ path: file.path, code: 'content_mismatch' })
    } catch {
      problems.push({ path: file.path, code: 'unreadable' })
    }
  }
  const paths = new Set(manifest.files.map((file) => file.path))
  for (const step of manifest.steps) {
    for (const kind of ['screenshot', 'accessibility', 'dom'])
      if (!paths.has(step.artifacts?.[kind]))
        problems.push({ step: step.id, code: 'missing_' + kind })
  }
  return { valid: problems.length === 0, problems }
}
