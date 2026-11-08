import type {
  Conversation,
  ConversationInput,
  PersonState,
} from '../../api/generated/types.gen'
import { OriginalImage } from '../../shared/images/original'
import { useFormatMessage, useMessages } from '../../shared/preferences'
import { Button } from '../../shared/ui/button'
import { Dialog, Modal } from '../../shared/ui/popup'

type Detail = [string, string]

function Facts({ items }: { items: Detail[] }) {
  return (
    <dl className="situation-facts">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export function CurrentSituation({
  conversation,
  showHeading,
}: {
  conversation: Conversation
  showHeading: boolean
}) {
  const m = useMessages()
  const format = useFormatMessage()
  const { state, definition } = conversation
  const current: Detail[] = [
    [m.currentPlace, state?.place ?? ''],
    [m.currentTime, state?.time ?? ''],
    [
      m.elapsed,
      state?.elapsed_minutes
        ? format('elapsedTime', { minutes: state.elapsed_minutes })
        : '',
    ],
    [m.currentGoal, state?.goal ?? ''],
    [
      m.currentIntention,
      state?.intention === state?.goal ? '' : (state?.intention ?? ''),
    ],
    [m.narrationLabel, state?.scenery ?? ''],
    [m.relationship, state?.relationship ?? ''],
  ]
  const visible = current.filter(([, value]) => value.trim())
  const people: [string, PersonState | undefined][] = [
    [definition.user?.name || m.you, state?.player],
    [definition.character?.name || m.character, state?.character],
  ]
  const peopleWithFacts = people.flatMap(([name, person], index) => {
    const fields = [
      [m.appearance, person?.appearance],
      [m.clothing, person?.clothing],
      [m.pose, person?.pose],
      [m.expression, person?.expression],
      [
        m.heldObjects,
        person?.held_objects?.map((item) => item.name).join(', '),
      ],
    ] as const
    const items: Detail[] = fields.flatMap(([label, value]) =>
      value == null ? [] : [[label, value.trim() || m.emptyValue]],
    )
    return items.length ? [{ name, items, index }] : []
  })
  return (
    <aside className="state-panel">
      {showHeading && <h2>{m.currentState}</h2>}
      {visible.length > 0 && <Facts items={visible} />}
      {peopleWithFacts.length > 0 && (
        <details>
          <summary>{m.peopleDetails}</summary>
          <div className="details-content">
            {peopleWithFacts.map(({ name, items, index }) => (
              <section className="situation-person" key={index}>
                <h3>{name}</h3>
                <Facts items={items} />
              </section>
            ))}
          </div>
        </details>
      )}
      {!visible.length && !peopleWithFacts.length && (
        <p className="subtle">{m.emptySituation}</p>
      )}
    </aside>
  )
}

export function StartedSettings({
  definition,
  screenHidden,
}: {
  definition: ConversationInput
  screenHidden: boolean
}) {
  const m = useMessages()
  const portrait = definition.presets?.character
  const story = definition.scenario
  const scenery = [story?.opening, story?.description, story?.world].find(
    (value) => value?.trim(),
  )
  return (
    <Dialog.Root>
      <Dialog.Trigger render={<Button />}>{m.startedSettings}</Dialog.Trigger>
      {!screenHidden && (
        <Modal label={m.startedSettings} closeLabel={m.closePanel}>
          <div className="details-content">
            {scenery && (
              <section>
                <h3>{m.scenario}</h3>
                <p className="state-description">{scenery}</p>
              </section>
            )}
            {(['user', 'character'] as const).map((role) => {
              const person = definition[role]
              if (!person) return null
              const items: Detail[] = [
                [m.age, person.age],
                [m.gender, person.gender],
                [m.appearance, person.appearance],
                [m.initial_clothing, person.initial_clothing],
              ].flatMap(([label, value]) =>
                value ? [[String(label), String(value)]] : [],
              )
              const description = person.description?.trim()
              return (
                <section className="situation-person" key={role}>
                  <h3>
                    {person.name || (role === 'user' ? m.you : m.character)}
                  </h3>
                  {description && (
                    <p className="state-description">{description}</p>
                  )}
                  {items.length > 0 && <Facts items={items} />}
                  {role === 'character' && portrait?.image_id && (
                    <OriginalImage
                      className="character-portrait"
                      identity={portrait.image_id}
                      alt={portrait.name}
                    />
                  )}
                </section>
              )
            })}
          </div>
        </Modal>
      )}
    </Dialog.Root>
  )
}
