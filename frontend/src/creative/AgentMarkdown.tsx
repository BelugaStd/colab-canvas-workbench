import { Fragment, type ReactNode } from 'react'

function safeLink(value: string): string | null {
  if (/[\u0000-\u0020\\]/u.test(value)) return null
  if (/^\/(?!\/)/u.test(value) || /^#project=[\w-]+$/u.test(value)) return value
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : null } catch { return null }
}

function inline(text: string, depth = 0): ReactNode[] {
  if (depth > 2) return [text]
  const result: ReactNode[] = [], expression = /\*\*([^*\n]+)\*\*|`([^`\n]+)`|\[([^\]\n]+)\]\(([^)\n]+)\)/gu
  let last = 0
  for (const match of text.matchAll(expression)) {
    if (match.index! > last) result.push(text.slice(last, match.index))
    const key = `${match.index}-${depth}`
    if (match[1]) result.push(<strong key={key}>{inline(match[1], depth + 1)}</strong>)
    else if (match[2]) result.push(<code key={key}>{match[2]}</code>)
    else {
      const href = safeLink(match[4])
      result.push(href ? <a key={key} href={href} target={href.startsWith('http') ? '_blank' : undefined} rel="noopener noreferrer">{match[3]}</a> : match[0])
    }
    last = match.index! + match[0].length
  }
  if (last < text.length) result.push(text.slice(last))
  return result
}

export default function AgentMarkdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [], lines = text.replaceAll('\r\n', '\n').split('\n')
  let paragraph: string[] = [], list: { ordered: boolean; content: string[] } | null = null
  const flushParagraph = () => { if (paragraph.length) { blocks.push(<p key={blocks.length}>{paragraph.map((line, index) => <Fragment key={index}>{index > 0 && <br/>}{inline(line)}</Fragment>)}</p>); paragraph = [] } }
  const flushList = () => { if (list) { const Tag = list.ordered ? 'ol' : 'ul'; blocks.push(<Tag key={blocks.length}>{list.content.map((item, index) => <li key={index}>{inline(item)}</li>)}</Tag>); list = null } }
  for (const line of lines) {
    if (!line.trim()) { flushParagraph(); flushList(); continue }
    const item = line.match(/^\s*(?:(\d+)\.\s+|[-*]\s+)(.+)$/u)
    if (item) {
      flushParagraph()
      const ordered = !!item[1]
      if (list && list.ordered !== ordered) flushList()
      list ??= { ordered, content: [] }; list.content.push(item[2])
    } else { flushList(); paragraph.push(line) }
  }
  flushParagraph(); flushList()
  return <div className="ca-markdown">{blocks}</div>
}
