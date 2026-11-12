import { useState, type ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { Presence, Reveal } from './Motion'

export default function Disclosure({ title, children, defaultOpen = false }: { title: string; children: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return <div className="cc-disclosure"><button aria-expanded={open} onClick={() => setOpen(!open)}><ChevronRight size={13} style={{rotate:open?'90deg':'0deg'}}/>{title}</button>
    <Presence>{open && <Reveal className="cc-disclosure-body">{children}</Reveal>}</Presence></div>
}
