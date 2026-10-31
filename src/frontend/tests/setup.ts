import '@testing-library/jest-dom/vitest'
import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
afterEach(cleanup)

Object.defineProperty(window, 'matchMedia', {
  value: vi.fn((query: string) => ({
    matches: query.includes('min-width'),
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
})
