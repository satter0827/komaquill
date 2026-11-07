import { Dialog } from '@base-ui/react/dialog'
import { Popover } from '@base-ui/react/popover'
import { X } from 'lucide-react'
import type { ComponentProps, ReactNode } from 'react'
import { useMessages } from '../preferences'
import { useMediaQuery } from '../viewport'
import { Button } from './button'

export { Dialog, Popover }

const SHORT_POPUP_VIEWPORT = '(max-height: 550px)'

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
  const m = useMessages()
  const short = useMediaQuery(SHORT_POPUP_VIEWPORT)
  return (
    <Popover.Portal keepMounted={keepMounted}>
      <Popover.Positioner
        side="top"
        align="start"
        sideOffset={8}
        collisionPadding={16}
        collisionAvoidance={
          short ? { side: 'shift', align: 'shift' } : undefined
        }
        className="popup-positioner"
      >
        <Popover.Popup
          {...props}
          data-roomy={short || undefined}
          className={`popup-content details-content ${className ?? ''}`}
        >
          <div className="popup-heading">
            <Popover.Title className="popup-title">{label}</Popover.Title>
            {short && (
              <Popover.Close
                render={<Button variant="ghost" aria-label={m.closePanel} />}
              >
                <X size={20} aria-hidden="true" />
              </Popover.Close>
            )}
          </div>
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
