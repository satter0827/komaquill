import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Image } from 'lucide-react'
import { useState } from 'react'
import * as q from '../api/generated/@tanstack/react-query.gen'
import type { ImageInput } from '../api/generated/types.gen'
import {
  useFailure,
  useFormatMessage,
  useMessages,
  usePreferences,
} from '../app/preferences'
import { Button } from '../components/ui/button'
import { imageOptions, operationOptions } from './operations'

export function ImageResult({
  identity,
  operationId,
}: {
  identity: string
  operationId?: string
}) {
  const messages = useMessages()
  const failure = useFailure()
  const formatMessage = useFormatMessage()
  const cache = useQueryClient()
  const operation = useQuery({
    ...operationOptions(operationId ?? ''),
    enabled: !!operationId,
  })

  const image = useQuery({
    enabled: !operationId || operation.data?.image_state === 'submitted',
    ...imageOptions(identity),
  })
  const collect = useMutation({
    ...q.collectImageMutation(),
    onSuccess: () =>
      cache.invalidateQueries({
        queryKey: q.getImageQueryKey({ path: { identity: identity ?? '' } }),
      }),
  })
  return (
    <div className="image-result">
      {image.data?.images?.map((_, index) => (
        <img
          key={index}
          src={`/api/v1/images/${identity}/files/${index}`}
          alt={formatMessage('imageAlt', { index: index + 1 })}
        />
      ))}
      <p className="subtle">
        {operation.data?.image_state === 'failed'
          ? failure({ code: operation.data.image_error })
          : image.data?.state === 'saved'
            ? messages.imageReady
            : messages.imagePreparing}
      </p>
      {(image.error || (image.data && image.data.state !== 'saved')) && (
        <Button
          onClick={() => collect.mutate({ path: { identity } })}
          disabled={collect.isPending}
        >
          {messages.retryCollection}
        </Button>
      )}
      {image.error && (
        <p role="alert" className="error">
          {failure(image.error)}
        </p>
      )}
      {collect.error && (
        <p role="alert" className="error">
          {failure(collect.error)}
        </p>
      )}
      {image.data?.failure && (
        <p className="error">{failure(image.data.failure)}</p>
      )}
    </div>
  )
}

export function ImagesPage() {
  const [prompt, setPrompt] = useState('')
  const [prepared, setPrepared] = useState<ImageInput | null>(null)
  const messages = useMessages()
  const failure = useFailure()
  const preferences = usePreferences()
  const cache = useQueryClient()
  const images = useQuery({
    ...q.listImagesOptions(),
  })
  const submit = useMutation({
    ...q.submitImageMutation(),
    onError: (error) => {
      const code =
        error && typeof error === 'object' && 'code' in error
          ? error.code
          : null
      if (code === 'real_llm_not_allowed' || code === 'fake_input_mismatch') {
        setPrepared(null)
      }
    },
    onSuccess: () => {
      setPrepared(null)
      return cache.invalidateQueries({ queryKey: q.listImagesQueryKey() })
    },
  })
  return (
    <section className="page">
      <div className="eyebrow">{messages.imageEyebrow}</div>
      <h1>{messages.images}</h1>
      <p className="subtle">{messages.imageDescription}</p>
      <form
        className="image-form"
        onSubmit={(event) => {
          event.preventDefault()
          const body = prepared ?? {
            request_id: crypto.randomUUID(),
            prompt,
            llm_profile: preferences.value.llm_profile,
            allow_real_llm: true,
          }
          setPrepared(body)
          submit.mutate({ body })
        }}
      >
        <label>
          {messages.imagePrompt}
          <textarea
            required
            rows={4}
            value={prompt}
            disabled={submit.isPending || (submit.isError && prepared !== null)}
            onChange={(event) => setPrompt(event.target.value)}
          />
        </label>
        <Button
          className="primary"
          type="submit"
          disabled={submit.isPending || !prompt.trim()}
        >
          <Image size={17} />
          {submit.isError && prepared
            ? messages.retryRequest
            : messages.generate}
        </Button>
      </form>
      {submit.error && (
        <p role="alert" className="error">
          {failure(submit.error)}
        </p>
      )}
      {images.error && (
        <p role="alert" className="error">
          {failure(images.error)}
        </p>
      )}
      <div className="image-grid">
        {images.data?.map((image) => (
          <ImageResult key={image.run_id} identity={image.run_id} />
        ))}
      </div>
    </section>
  )
}
