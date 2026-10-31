import { createHash, randomUUID } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import path from 'node:path'

export const hash = (value) => createHash('sha256').update(value).digest('hex')
const time = () => new Date().toISOString()
const messages = JSON.parse(
  readFileSync(new URL('../messages.json', import.meta.url), 'utf8'),
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
      'duplicate',
      'start',
      'stop',
      'new',
      'assets',
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
  await mkdir(path.dirname(file), { recursive: true })
  const temporary = file + '.' + randomUUID() + '.tmp'
  await writeFile(temporary, JSON.stringify(value, null, 2) + '\n', {
    flag: 'wx',
  })
  await rename(temporary, file)
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
      'scripts/python.mjs',
      '-c',
      'from pathlib import Path; from scripts._support import input_fingerprint; print(input_fingerprint(Path.cwd()))',
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
    this.listeners = []
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
      }),
    )
    this.listen('response', (response) =>
      add('response', {
        method: response.request().method(),
        route: route(response.url()),
        status: response.status(),
      }),
    )
    this.listen('requestfailed', (request) =>
      add('request_failed', {
        method: request.method(),
        route: route(request.url()),
      }),
    )
    this.listen('console', (message) =>
      add('console', {
        level: message.type(),
        fingerprint: hash(message.text()),
      }),
    )
    this.listen('pageerror', (error) => add('page_error', diagnostic(error)))
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
    Object.assign(this.manifest, {
      status,
      expected_status: expectedStatus,
      outcome: classify(status, expectedStatus, this.manifest.mode),
      finished_at: time(),
    })
    await this.checkpoint()
    return this.manifest
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
