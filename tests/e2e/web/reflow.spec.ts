import { test, expect } from './fixtures'
import ja from '../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

for (const locale of ['ja', 'en'] as const) {
  for (const density of ['comfortable', 'compact'] as const) {
    test(`${locale} ${density}: queued actions leave the composer usable after reflow`, async ({
      page,
      request,
      evidence,
    }) => {
      const messages = locale === 'ja' ? ja : en
      const preferences = await (
        await request.get('/api/v1/preferences')
      ).json()
      await request.put('/api/v1/preferences', {
        data: { ...preferences, locale, density },
      })
      const scene = await (
        await request.post('/api/v1/conversations', {
          data: {
            title: locale === 'ja' ? '雨の日の古書店' : 'A rainy afternoon',
            llm_profile: 'lmstudio-4b',
          },
        })
      ).json()
      await page.goto(`/conversations/${scene.id}`)
      const input = page.getByRole('textbox', { name: messages.messageLabel })
      await input.fill('May we wait here for five minutes?')
      await page
        .getByRole('button', { name: messages.addAction, exact: true })
        .click()
      await page.getByLabel(messages.chooseAction).selectOption('wait')
      await page.getByLabel(messages.waitMinutes).fill('5')
      await page
        .getByRole('button', { name: messages.queueAction, exact: true })
        .click()
      for (const fontSize of ['100%', '200%']) {
        await page.addStyleTag({
          content: `html{font-size:${fontSize} !important}`,
        })
        for (const viewport of [
          { width: 390, height: 420 },
          { width: 390, height: 844 },
          { width: 1440, height: 960 },
        ]) {
          await page.setViewportSize(viewport)
          await expect(input).toBeInViewport({ ratio: 1 })
          await expect(
            page.getByRole('button', { name: messages.send, exact: true }),
          ).toBeInViewport({ ratio: 1 })
          if (fontSize === '100%' || viewport.height > 550) {
            await expect(page.locator('.queued-action > p')).toBeInViewport({
              ratio: 1,
            })
          }
          expect(
            await page.evaluate(() => document.documentElement.scrollWidth),
          ).toBeLessThanOrEqual(viewport.width)
          const navigation = page.locator('.workspace > .navigation')
          if (await navigation.count()) {
            expect(
              await navigation.evaluate(
                (element) => element.scrollWidth - element.clientWidth,
              ),
            ).toBeLessThanOrEqual(1)
          }
          await evidence.capture(
            `Queued action at ${viewport.width}x${viewport.height}, text ${fontSize}`,
          )
        }
      }
      await page.setViewportSize({ width: 390, height: 844 })
      await page.getByRole('button', { name: messages.showNavigation }).click()
      const navigation = page.locator('.navigation-modal .navigation')
      expect(
        await navigation.evaluate(
          (element) => element.scrollWidth - element.clientWidth,
        ),
      ).toBeLessThanOrEqual(1)
      await page
        .getByRole('button', { name: messages.closePanel, exact: true })
        .click()
      await page
        .getByRole('button', { name: messages.removeQueuedAction, exact: true })
        .click()
      await page
        .getByRole('button', { name: messages.addAction, exact: true })
        .click()
      await page.getByLabel(messages.chooseAction).selectOption('wait')
      await page.getByLabel(messages.waitMinutes).fill('5')
      await page
        .getByRole('button', { name: messages.queueAction, exact: true })
        .click()
      await expect(page.locator('.queued-action > p')).toBeInViewport({
        ratio: 1,
      })
      await expect(input).toHaveValue('May we wait here for five minutes?')
      await page
        .getByRole('button', { name: messages.editQueuedAction, exact: true })
        .click()
      await expect(page.getByLabel(messages.waitMinutes)).toHaveValue('5')
      await page.keyboard.press('Escape')
      await expect(input).toBeFocused()
      await expect(input).toBeInViewport({ ratio: 1 })
      await page
        .getByRole('button', { name: messages.send, exact: true })
        .click()
      await expect(page.locator('.assistant-message')).toHaveCount(1)
      const saved = await (
        await request.get(`/api/v1/conversations/${scene.id}`)
      ).json()
      expect(saved.state.elapsed_minutes).toBe(5)
      expect(
        await (
          await request.get(`/api/v1/conversations/${scene.id}/actions`)
        ).json(),
      ).toHaveLength(1)
    })
  }
}
