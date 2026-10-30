import en from './locales/en.json'
import ja from './locales/ja.json'

export type Locale = 'ja' | 'en'
export type Messages = typeof en
export type MessageKey = Exclude<keyof Messages, 'errors'>
const catalogs: Record<Locale, Messages> = { ja, en }

export function messageCatalog(locale: Locale): Messages {
  return catalogs[locale]
}

export function formatMessage(
  key: MessageKey,
  values: Record<string, string | number>,
  locale: Locale = 'ja',
) {
  return catalogs[locale][key].replace(/\{(\w+)\}/g, (_, name: string) =>
    String(values[name]),
  )
}
