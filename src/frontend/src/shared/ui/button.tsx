import { Button as Primitive } from '@base-ui/react/button'
import { cva, type VariantProps } from 'class-variance-authority'
import { clsx } from 'clsx'
import type { ComponentProps } from 'react'

export const buttonStyle = cva('button', {
  variants: {
    variant: {
      primary: 'primary',
      secondary: null,
      ghost: 'ghost',
      danger: 'danger',
    },
  },
  defaultVariants: { variant: 'secondary' },
})

export function Button({
  className,
  variant,
  ...props
}: ComponentProps<typeof Primitive> & VariantProps<typeof buttonStyle>) {
  return (
    <Primitive
      className={clsx(buttonStyle({ variant }), className)}
      {...props}
    />
  )
}
