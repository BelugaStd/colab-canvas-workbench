import { useLayoutEffect, useRef, useState, type ComponentProps, type Ref } from 'react'

function useDraft(value: string, change: (value: string) => void) {
  const [draft, setDraft] = useState(value)
  const pending = useRef<string[]>([]), composing = useRef(false)
  useLayoutEffect(() => {
    const acknowledged = pending.current.lastIndexOf(value)
    if (acknowledged >= 0) {
      pending.current = pending.current.slice(acknowledged + 1)
    } else if (!composing.current) {
      pending.current = []
      setDraft(value)
    }
  }, [value])
  function commit(next: string) {
    pending.current.push(next)
    change(next)
  }
  return {
    value: draft,
    onChange: (event: { currentTarget: { value: string } }) => {
      const next = event.currentTarget.value
      // WARN: Animated parents can echo older props; the input must acknowledge each keystroke locally.
      setDraft(next)
      if (!composing.current) commit(next)
    },
    onCompositionStart: () => { composing.current = true },
    onCompositionEnd: (event: { currentTarget: { value: string } }) => {
      composing.current = false
      setDraft(event.currentTarget.value)
      commit(event.currentTarget.value)
    },
  }
}

type DraftProps = { value: string; onValueChange: (value: string) => void }

export function DraftTextarea({ value, onValueChange, ...props }: Omit<ComponentProps<'textarea'>, 'value'|'onChange'> & DraftProps & { ref?: Ref<HTMLTextAreaElement> }) {
  const draft = useDraft(value, onValueChange)
  return <textarea {...props} {...draft}/>
}

export function DraftInput({ value, onValueChange, ...props }: Omit<ComponentProps<'input'>, 'value'|'onChange'> & DraftProps & { ref?: Ref<HTMLInputElement> }) {
  const draft = useDraft(value, onValueChange)
  return <input {...props} {...draft}/>
}
