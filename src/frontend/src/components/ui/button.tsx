import { Button as Primitive } from '@base-ui/react/button'
import { clsx } from 'clsx'
import type { ComponentProps } from 'react'

export function Button({
  className,
  ...props
}: ComponentProps<typeof Primitive>) {
  return <Primitive className={clsx('button', className)} {...props} />
}
