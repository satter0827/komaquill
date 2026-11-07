import type { Locale } from '../messages'

export function presetsForLocale<
  T extends { id: string; sample?: boolean; sample_locale: Locale | null },
>(presets: T[] | undefined, locale: Locale, selectedId?: string | null) {
  return (presets ?? []).filter(
    (preset) =>
      !preset.sample ||
      preset.sample_locale === locale ||
      preset.id === selectedId,
  )
}
