import { messageCatalog } from '../app/messages'
import type { Locale } from '../app/messages'
import { client } from './generated/client.gen'
client.setConfig({ baseUrl: '', throwOnError: true })

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
