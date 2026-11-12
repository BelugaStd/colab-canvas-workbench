import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown } from 'lucide-react'
import { Presence, Surface } from './Motion'

export default function GlassSelect({ label, value, options, change }: { label: string; value: string;
  options: { value: string; label: string }[]; change: (v: string) => void }) {
  const [open, setOpen] = useState(false), [index, setIndex] = useState(0)
  const [position, setPosition] = useState({ left: 0, top: 0, width: 140 })
  const anchor = useRef<HTMLButtonElement>(null), menu = useRef<HTMLDivElement>(null), id = useId()
  function choose(v: string) { change(v); setOpen(false); anchor.current?.focus() }
  useLayoutEffect(() => {
    if (!open || !anchor.current) return
    function align() {
      const r = anchor.current!.getBoundingClientRect(), width = Math.max(140, r.width)
      setPosition({ left: Math.max(8, Math.min(r.left, innerWidth-width-8)), top: Math.max(8, Math.min(r.bottom+6, innerHeight-options.length*36-24)), width })
    }
    align(); setIndex(Math.max(0, options.findIndex(o => o.value===value)))
    window.addEventListener('resize', align); window.addEventListener('scroll', align, true)
    return () => { window.removeEventListener('resize', align); window.removeEventListener('scroll', align, true) }
  }, [open, options.length, value])
  useEffect(() => {
    if (!open) return
    const dismiss = (e: PointerEvent) => { if (!anchor.current?.contains(e.target as Node) && !menu.current?.contains(e.target as Node)) setOpen(false) }
    window.addEventListener('pointerdown', dismiss, true)
    return () => window.removeEventListener('pointerdown', dismiss, true)
  }, [open])
  return <><button ref={anchor} type="button" role="combobox" className="cc-glass-select nodrag nopan" aria-label={label}
    aria-expanded={open} aria-controls={id} aria-activedescendant={open?`${id}-${index}`:undefined}
    onBlur={e=>{if(!menu.current?.contains(e.relatedTarget as Node))setOpen(false)}} onClick={() => setOpen(!open)} onKeyDown={e => {
      if (e.key==='Escape') { e.stopPropagation(); setOpen(false) }
      if (['ArrowDown','ArrowUp','Home','End','Enter',' '].includes(e.key)) {
        e.preventDefault(); e.stopPropagation()
        if (!open) { setOpen(true); return }
        if (e.key==='Enter'||e.key===' ') choose(options[index].value)
        else setIndex(i => e.key==='Home'?0:e.key==='End'?options.length-1:(i+(e.key==='ArrowUp'?-1:1)+options.length)%options.length)
      }
    }}>{options.find(o => o.value===value)?.label}<ChevronDown size={12}/></button>
    {createPortal(<Presence>{open && <Surface ref={menu} id={id} role="listbox" aria-label={label} className="cc-glass-options nodrag nopan" style={position} onKeyDown={e=>{if(e.key==='Escape'){e.stopPropagation();setOpen(false);anchor.current?.focus()}}}>
      {options.map((o,i) => <button type="button" key={o.value} id={`${id}-${i}`} role="option" aria-selected={o.value===value}
        className={index===i?'active':''} onPointerEnter={() => setIndex(i)} onClick={() => choose(o.value)}>{o.label}{o.value===value&&<Check size={13}/>}</button>)}
    </Surface>}</Presence>, document.querySelector('.creative-app') ?? document.body)}</>
}
