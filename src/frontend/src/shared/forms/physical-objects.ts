import type { PhysicalObject } from '../../api/generated/types.gen'

export function objectLines(value: PhysicalObject[] | null | undefined) {
  return value?.map((item) => item.name).join('\n') ?? ''
}

export function parseObjects(
  text: string,
  previous: PhysicalObject[] = [],
): PhysicalObject[] {
  const available = [...previous]
  return text
    .split('\n')
    .map((name) => name.trim())
    .filter(Boolean)
    .map((name) => {
      const index = available.findIndex((item) => item.name === name)
      return index >= 0
        ? available.splice(index, 1)[0]
        : { id: crypto.randomUUID(), name }
    })
}
