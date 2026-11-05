export function StoryReply({
  narration,
  action,
  dialogue,
  name,
}: {
  narration?: string
  action?: string
  dialogue?: string
  name: string
}) {
  return (
    <>
      {narration && <p className="scene-narration">{narration}</p>}
      {action && <p className="action-narration">{action}</p>}
      {dialogue && (
        <div className="dialogue">
          <span className="message-label">{name}</span>
          <p>{dialogue}</p>
        </div>
      )}
    </>
  )
}
