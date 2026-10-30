import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { '/api': { target: 'http://127.0.0.1:8765' } } },
  build: { outDir: '../../.artifacts/build/frontend', emptyOutDir: true },
  cacheDir: '../../.artifacts/cache/vite',
  test: {
    environment: 'jsdom',
    setupFiles: './tests/setup.ts',
    include: ['tests/**/*.test.tsx'],
  },
})
