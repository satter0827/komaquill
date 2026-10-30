import { Button as Primitive } from '@base-ui/react/button'
import type { ComponentProps } from 'react'
import { clsx } from 'clsx'

export function Button({
  className,
  ...props
}: ComponentProps<typeof Primitive>) {
  return <Primitive className={clsx('button', className)} {...props} />
}
