import messages from './messages.json'

export function formatMessage(
  key: keyof typeof messages,
  values: Record<string, string | number>,
) {
  return messages[key].replace(/\{(\w+)\}/g, (_, name: string) =>
    String(values[name]),
  )
}
