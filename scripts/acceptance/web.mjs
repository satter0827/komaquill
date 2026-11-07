import assert from 'node:assert/strict'
import path from 'node:path'
import { readFile } from 'node:fs/promises'
import { chromium } from '@playwright/test'
import ja from '../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../src/frontend/src/app/locales/en.json' with { type: 'json' }
import {
  Evidence,
  contained,
  diagnostic,
  jsonFile,
} from '../../tools/testing/web/evidence.mjs'

const configPath = contained(path.resolve('.artifacts'), process.argv[2])
const config = JSON.parse(await readFile(configPath, 'utf8'))
assert.equal(config.format_version, 1)
assert.equal(typeof config.real_llm, 'boolean')
const origin = new URL(config.endpoint)
assert(['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname))
assert(!origin.username && !origin.password && !origin.search && !origin.hash)
const directory = path.dirname(configPath)
const browser = await chromium.launch()
const page = await browser.newPage({
  baseURL: origin.origin,
  viewport: { width: 1440, height: 960 },
})
page.setDefaultTimeout(30000)
const evidence = new Evidence(page, path.join(directory, 'browser'), {
  mode: 'assertion',
  real_llm: config.real_llm,
  real_images: config.has_images,
})
let status = 'failed'
try {
  for (const identity of config.conversations) {
    await evidence.step('Read a saved conversation after restart', async () => {
      await page.goto(`/conversations/${identity}`)
      const expected = config.conversation_turn_counts[identity]
      assert(Number.isInteger(expected) && expected > 0)
      await page
        .locator('.assistant-message')
        .nth(Math.min(expected, 30) - 1)
        .waitFor()
      while ((await page.locator('.assistant-message').count()) < expected) {
        await page
          .getByRole('button', { name: ja.olderTurns, exact: true })
          .or(page.getByRole('button', { name: en.olderTurns, exact: true }))
          .click()
        await page
          .locator('.assistant-message')
          .nth(expected - 1)
          .waitFor()
      }
      assert.equal(await page.locator('.assistant-message').count(), expected)
    })
  }
  if (config.has_images) {
    await evidence.step('Read saved images', async () => {
      await page.goto('/images')
      const days = page.locator('.album-days > details')
      const images = page.locator('.album-days img')
      await days.first().waitFor()
      for (const day of await days.all()) {
        if (await images.count()) break
        if ((await day.getAttribute('open')) === null)
          await day.locator('summary').click()
      }
      await images.first().waitFor()
      assert((await images.count()) > 0)
    })
  }
  for (const view of config.standard_views) {
    await evidence.step(`Inspect standard ComfyUI ${view.view}`, async () => {
      // The pinned frontend treats absent user styles, templates and directories as empty.
      // Only these read-only 404 contracts are optional; all other failures remain fatal.
      const stopExpectations = [
        ['/user.css', 1],
        ['/api/userdata/user.css', 1],
        ['/api/userdata/comfy.templates.json', 1],
        ['/api/userdata', 4],
        ['/api/global_subgraphs', 1],
      ].map(([route, maximum]) =>
        evidence.expectFailure({
          kind: 'response',
          route,
          method: 'GET',
          status: 404,
          maximum,
          origin: view.url,
        }),
      )
      try {
        await page.goto(view.url)
        await page
          .getByRole('status', { name: 'Loading ComfyUI' })
          .waitFor({ state: 'hidden', timeout: 90000 })
        const ready = await page.waitForFunction(
          async (expected) => {
            const { app } = await import(
              new URL('scripts/app.js', location.href).href
            )
            const graph = app.graph
            if (
              !graph?._nodes?.length ||
              (graph.extra?.linearMode === true) !== expected
            )
              return false
            return {
              nodes: graph._nodes.length,
              linear: graph.extra?.linearMode === true,
            }
          },
          view.view === 'app',
          { timeout: 90000 },
        )
        const graph = await ready.jsonValue()
        assert(graph.nodes > 0)
        assert.equal(graph.linear, view.view === 'app')
        await page.waitForLoadState('networkidle')
        const skipTour = page.getByRole('button', { name: /^(Skip|スキップ)$/ })
        if (await skipTour.isVisible()) {
          await skipTour.click()
          await skipTour.waitFor({ state: 'hidden' })
        }
      } finally {
        for (const stop of stopExpectations) stop()
      }
    })
  }
  status = 'passed'
} catch (error) {
  await jsonFile(
    path.join(directory, 'browser-failure.json'),
    diagnostic(error),
  )
} finally {
  await evidence.capture('Final saved result')
  const result = await evidence.finish(status, 'passed')
  await jsonFile(path.join(directory, 'browser-result.json'), result)
  await browser.close()
  if (result.unexpected_events.length) process.exitCode = 1
  if (status !== 'passed') process.exitCode = 1
}
