import { useState } from 'react'
import type { ImageInput } from '../api/generated/types.gen'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Image } from 'lucide-react'
import {
  collectImage,
  getImage,
  getOperation,
  listImages,
  submitImage,
} from '../api/generated/sdk.gen'
import { failure, POLL_INTERVAL_MS } from '../api/client'
import { Button } from '../components/ui/button'
import messages from '../app/messages.json'
import { formatMessage } from '../app/messages'

export function ImageResult({
  identity,
  operationId,
}: {
  identity: string
  operationId?: string
}) {
  const cache = useQueryClient()
  const operation = useQuery({
    queryKey: ['operation', operationId],
    enabled: !!operationId,
    queryFn: async () =>
      (await getOperation({ path: { identity: operationId! } })).data!,
    refetchInterval: (query) =>
      query.state.data?.image_state === 'pending' ? POLL_INTERVAL_MS : false,
  })
  const image = useQuery({
    enabled: !operationId || operation.data?.image_state === 'submitted',
    queryKey: ['image', identity],
    queryFn: async () => (await getImage({ path: { identity } })).data!,
    refetchInterval: (query) =>
      query.state.error ||
      ['saved', 'failed', 'lost'].includes(query.state.data?.state ?? '')
        ? false
        : POLL_INTERVAL_MS,
  })
  const collect = useMutation({
    mutationFn: () => collectImage({ path: { identity } }),
    onSuccess: () => cache.invalidateQueries({ queryKey: ['image', identity] }),
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
          ? (operation.data.image_error_message ??
            formatMessage('imageFailed', {
              code: operation.data.image_error ?? 'operation_failed',
            }))
          : (image.data?.state ?? messages.imageWaiting)}
      </p>
      {image.data && image.data.state !== 'saved' && (
        <Button onClick={() => collect.mutate()} disabled={collect.isPending}>
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
        <p className="error">{image.data.failure.code}</p>
      )}
    </div>
  )
}

export function ImagesPage() {
  const [prompt, setPrompt] = useState('')
  const [prepared, setPrepared] = useState<ImageInput | null>(null)
  const cache = useQueryClient()
  const images = useQuery({
    queryKey: ['images'],
    queryFn: async () => (await listImages()).data!,
  })
  const submit = useMutation({
    mutationFn: (body: ImageInput) => submitImage({ body }),
    onSuccess: () => {
      setPrepared(null)
      return cache.invalidateQueries({ queryKey: ['images'] })
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
          const body = prepared ?? { request_id: crypto.randomUUID(), prompt }
          setPrepared(body)
          submit.mutate(body)
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
          {submit.isError ? messages.retryRequest : messages.generate}
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
