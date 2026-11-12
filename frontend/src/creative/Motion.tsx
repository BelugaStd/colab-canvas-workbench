import { AnimatePresence, motion, type HTMLMotionProps } from 'motion/react'
import { useEffect, useRef, type ReactNode } from 'react'

export const ease = [0.22, 1, 0.36, 1] as const
export const transition = { duration: .18, ease }
export function Presence({ children, mode = 'sync' }: { children: ReactNode; mode?: 'sync' | 'wait' }) {
  return <AnimatePresence initial={false} mode={mode}>{children}</AnimatePresence>
}
export function Surface({ tone = 'pop', ...props }: HTMLMotionProps<'div'> & { tone?: 'pop' | 'fade' }) {
  const hidden = tone === 'fade' ? { opacity: 0 } : { opacity: 0, y: 6, scale: .98 }
  return <motion.div initial={hidden} animate={{ opacity: 1, y: 0, scale: 1 }} exit={hidden} transition={transition} {...props} />
}
export function Reveal({ children, ...props }: HTMLMotionProps<'div'>) {
  return <motion.div initial={{ height: 0, opacity: 0, overflow: 'hidden' }} animate={{ height: 'auto', opacity: 1, transitionEnd: { overflow: 'visible' } }}
    exit={{ height: 0, opacity: 0, overflow: 'hidden' }} transition={{ ...transition, duration: .22 }} {...props}>{children}</motion.div>
}

export function useDismiss(active: boolean, close: () => void) {
  const latest = useRef(close); latest.current = close
  useEffect(() => {
    if (!active) return
    const key = (e: KeyboardEvent) => { if (e.key==='Escape' && !document.querySelector('[role="listbox"]')) latest.current() }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [active])
}
