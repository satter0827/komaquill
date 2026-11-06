import { defineConfig } from '@playwright/test'
import { webConfig } from '../../../tools/testing/web/config.mjs'
export default defineConfig(webConfig('exploration'))
