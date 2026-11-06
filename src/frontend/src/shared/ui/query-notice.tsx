import { useFailure, useMessages } from '../preferences'
import { Button } from './button'

export function QueryNotice({
  error,
  retained = false,
  pending,
  onRetry,
}: {
  error: unknown
  retained?: boolean
  pending: boolean
  onRetry: () => void
}) {
  const messages = useMessages()
  const failure = useFailure()
  if (!error) return null
  return (
    <div role="alert" className="query-notice">
      <p>{retained ? messages.refreshFailed : failure(error)}</p>
      <Button disabled={pending} onClick={onRetry}>
        {messages.reloadData}
      </Button>
    </div>
  )
}
