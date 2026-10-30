import messages from '../app/messages.json'
import { client } from './generated/client.gen'
client.setConfig({ baseUrl: '', throwOnError: true })

export function failure(error: unknown): string {
  if (
    error &&
    typeof error === 'object' &&
    'message' in error &&
    typeof error.message === 'string'
  )
    return error.message
  return messages.failed
}

export const POLL_INTERVAL_MS = 1000
