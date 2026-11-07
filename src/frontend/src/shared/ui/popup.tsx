import { Dialog } from '@base-ui/react/dialog'
import { Popover } from '@base-ui/react/popover'
import { X } from 'lucide-react'
import type { ComponentProps, ReactNode } from 'react'
import { Button } from './button'

export { Dialog, Popover }

export function Popup({
  label,
  children,
  className,
  keepMounted = false,
  ...props
}: ComponentProps<typeof Popover.Popup> & {
  label: string
  keepMounted?: boolean
}) {
  return (
    <Popover.Portal keepMounted={keepMounted}>
      <Popover.Positioner
        side="top"
        align="start"
        sideOffset={8}
        collisionPadding={16}
        className="popup-positioner"
      >
        <Popover.Popup
          {...props}
          className={`popup-content details-content ${className ?? ''}`}
        >
          <Popover.Title className="popup-title">{label}</Popover.Title>
          {children}
        </Popover.Popup>
      </Popover.Positioner>
    </Popover.Portal>
  )
}

export function Modal({
  label,
  closeLabel,
  children,
  className,
  ...props
}: ComponentProps<typeof Dialog.Popup> & {
  label: string
  closeLabel: string
  children: ReactNode
}) {
  return (
    <Dialog.Portal>
      <Dialog.Backdrop className="modal-backdrop" />
      <Dialog.Popup {...props} className={`modal-content ${className ?? ''}`}>
        <div className="modal-heading">
          <Dialog.Title>{label}</Dialog.Title>
          <Dialog.Close
            render={<Button variant="ghost" aria-label={closeLabel} />}
          >
            <X size={20} aria-hidden="true" />
          </Dialog.Close>
        </div>
        {children}
      </Dialog.Popup>
    </Dialog.Portal>
  )
}
