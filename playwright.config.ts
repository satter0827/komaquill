import { defineConfig } from '@playwright/test'
import { webConfig } from './scripts/web/config.mjs'
export default defineConfig(webConfig())
