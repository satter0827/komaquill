import { useState } from 'react'
import { useMessages, useFormatMessage } from '../preferences'
import { Button } from '../ui/button'

export function OriginalImage({
  identity,
  index = 0,
  alt,
  className,
}: {
  identity: string
  index?: number
  alt?: string
  className?: string
}) {
  const m = useMessages()
  const format = useFormatMessage()
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const url = `/api/v1/images/${identity}/files/${index}`
  const failed = failedUrl === url
  return (
    <div className="image-file" data-failed={failed || undefined}>
      {!failed && (
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={alt || format('openImageLabel', { index: index + 1 })}
        >
          <img
            key={attempt}
            className={className}
            src={url}
            alt={alt || format('imageAlt', { index: index + 1 })}
            onError={() => setFailedUrl(url)}
            onLoad={() => setFailedUrl(null)}
          />
        </a>
      )}
      {failed && (
        <div className="image-error" role="alert">
          <p>{m.imageDisplayFailed}</p>
          <Button
            onClick={() => {
              setFailedUrl(null)
              setAttempt((value) => value + 1)
            }}
          >
            {m.retryCollection}
          </Button>
        </div>
      )}
    </div>
  )
}
