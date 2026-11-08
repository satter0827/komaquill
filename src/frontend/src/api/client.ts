import { messageCatalog } from '../shared/messages'
import type { Locale } from '../shared/messages'
import { client } from './generated/client.gen'

export const API_READ_TIMEOUT_MS = 30_000
export const API_REQUEST_TIMEOUT_MS = 15 * 60_000

client.setConfig({ baseUrl: '', throwOnError: true })
client.interceptors.request.use((request, { signal }) => {
  const deadline = AbortSignal.timeout(
    ['GET', 'HEAD'].includes(request.method)
      ? API_READ_TIMEOUT_MS
      : API_REQUEST_TIMEOUT_MS,
  )
  return new Request(request, {
    signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
  })
})

export function failure(error: unknown, locale: Locale = 'ja'): string {
  const messages = messageCatalog(locale)
  if (
    error &&
    typeof error === 'object' &&
    'code' in error &&
    typeof error.code === 'string'
  )
    return (
      messages.errors[error.code as keyof typeof messages.errors] ??
      messages.failed
    )
  if (error instanceof Error && error.message === messages.invalidState)
    return error.message
  return messages.failed
}

export const POLL_INTERVAL_MS = 1000

const REJECTED_REQUEST_CODES = new Set([
  'invalid_request',
  'request_not_found',
  'operation_conflict',
  'operation_busy',
  'visual_review_required',
  'image_description_invalid',
  'real_llm_not_allowed',
  'fake_input_mismatch',
  'invalid_configuration',
  'authentication_failed',
  'llm_dependencies_missing',
  'llm_unavailable',
  'connection_failed',
  'image_backend_unavailable',
  'stopping',
])

export function isRejectedRequest(error: unknown): boolean {
  return (
    !!error &&
    typeof error === 'object' &&
    'code' in error &&
    (('stage' in error && error.stage === 'connection') ||
      REJECTED_REQUEST_CODES.has(String(error.code)))
  )
}
