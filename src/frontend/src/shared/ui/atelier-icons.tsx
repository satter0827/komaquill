import type { ReactNode } from 'react'

function AtelierIcon({
  size,
  children,
}: {
  size: number
  children: ReactNode
}) {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  )
}

export function AtelierMark({ size = 24 }: { size?: number }) {
  return (
    <AtelierIcon size={size}>
      <path d="M14 4H3v16h11l3-3V4M17 6h4v12h-2" />
      <path d="m7 16 1-5 5-3-1 5-5 3Zm0 0 3-3" />
    </AtelierIcon>
  )
}

export function DrawIcon({ size = 18 }: { size?: number }) {
  return (
    <AtelierIcon size={size}>
      <path d="M11 4H3v16h15l3-3v-5" />
      <path d="m8 15 1-5 8-6 3 3-6 8-6 1Zm0 1 4-4" />
    </AtelierIcon>
  )
}

export function AlbumIcon({ size = 18 }: { size?: number }) {
  return (
    <AtelierIcon size={size}>
      <path d="M7 4h14v11M3 8h14v9l-3 3H3V8Z" />
      <path d="m6 16 3-3 3 3" />
    </AtelierIcon>
  )
}
