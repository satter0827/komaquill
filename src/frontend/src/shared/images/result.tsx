import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { Run, WorkItem } from '../../api/generated/types.gen'
import { useMessages, useFailure } from '../preferences'
import { WorkActions } from '../ui/work-actions'
import { Button } from '../ui/button'
import { imageOptions, isTerminal } from '../operations'
import {
  imageMonitorKey,
  useImageMonitorError,
  useTrackImage,
} from '../image-activity'
import { useAvailability } from '../availability'
import { OriginalImage } from './original'

export function ImageResult({
  identity,
  run,
  excluded = [],
  outputs,
}: {
  identity: string
  run?: Run
  excluded?: number[]
  outputs?: WorkItem[]
}) {
  const m = useMessages()
  const failure = useFailure()
  const cache = useQueryClient()
  const track = useTrackImage()
  const read = useQuery({ ...imageOptions(identity), enabled: !run })
  const monitorError = useImageMonitorError(identity)
  const value = run ?? read.data
  const collectionFailed =
    value?.collection_failure ||
    monitorError ||
    (value?.error === 'collection_failed' ? value.failure : null)
  const availability = useAvailability(
    collectionFailed ? { kind: 'collection', identity } : undefined,
  )
  return (
    <div className="image-result" id={`image-${identity}`} tabIndex={-1}>
      {outputs
        ? outputs.map((output) => (
            <div key={output.index}>
              <OriginalImage
                identity={identity}
                index={output.index}
                alt={output.revision ? output.name : undefined}
              />
              {!!output.revision && <p>{output.name}</p>}
              <WorkActions
                target={{ kind: 'image', id: identity, index: output.index }}
              />
            </div>
          ))
        : value?.images?.map(
            (filename, index) =>
              !excluded.includes(index) && (
                <OriginalImage
                  key={filename}
                  identity={identity}
                  index={index}
                />
              ),
          )}
      {value &&
        !value.images?.length &&
        !value.failure &&
        !isTerminal(value.state) && (
          <p className="subtle">{m.imagePreparing}</p>
        )}
      {(value?.failure || value?.state === 'failed') && (
        <p role="alert" className="error">
          {failure(
            value?.failure ?? { code: value?.error ?? 'generation_failed' },
          )}
        </p>
      )}
      {value?.state === 'lost' && (
        <p role="alert" className="error">
          {m.drawingResultUnknown}
        </p>
      )}
      {collectionFailed && (
        <div role="alert" className="image-error">
          <p>{failure(collectionFailed)}</p>
          <Button
            disabled={!availability.ready}
            onClick={() => {
              track(identity)
              void cache.invalidateQueries({
                queryKey: imageMonitorKey(identity),
              })
            }}
          >
            {m.retryCollection}
          </Button>
        </div>
      )}
      {read.error && !run && (
        <div role="alert" className="image-error">
          <p>{failure(read.error)}</p>
          <Button onClick={() => void read.refetch()}>
            {m.retryCollection}
          </Button>
        </div>
      )}
    </div>
  )
}
