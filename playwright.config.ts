import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './tests/e2e/web',
  outputDir: '.artifacts/records/reports/playwright/results',
  reporter: [
    ['list'],
    [
      'html',
      {
        outputFolder: '.artifacts/records/reports/playwright/html',
        open: 'never',
      },
    ],
  ],
  use: {
    baseURL: 'http://127.0.0.1:8766',
    viewport: { width: 1440, height: 960 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'node scripts/python.mjs -m tests.support.web_server',
    url: 'http://127.0.0.1:8766',
    reuseExistingServer: false,
    timeout: 30000,
  },
})
