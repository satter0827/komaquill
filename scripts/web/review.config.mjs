import { defineConfig } from '@playwright/test'
import { webConfig } from './config.mjs'
export default defineConfig(webConfig('exploration'))
