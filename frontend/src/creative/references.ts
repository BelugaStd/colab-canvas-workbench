import type { Edge } from '@xyflow/react'
import type { CardData, CanvasData, CreativeNode } from './types'
import { imageModel } from './imageModels'

export function referencesFor(id: string, canvas: Pick<CanvasData, 'nodes'|'edges'>) {
  let images = 0, texts = 0
  return canvas.edges.filter(e => e.target === id).flatMap(edge => {
    const node = canvas.nodes.find(n => n.id === edge.source)
    if (!node) return []
    const text = node.data.kind === 'text', number = text ? ++texts : ++images
    return [{ node, edge, alias: String(edge.data?.alias ?? `${text?'文本':'图'}${number}`), role: String(edge.data?.role ?? 'reference') }]
  })
}

export function normalizeReferences(canvas: CanvasData): CanvasData {
  let changed = false
  const edges = [...canvas.edges], nodes = canvas.nodes.map(node => {
    const refs = referencesFor(node.id, canvas)
    let sequence = Math.max(Number(node.data.reference_sequence ?? 0), ...refs.filter(r=>r.edge.data?.alias).map(r=>Number(r.alias.match(/\d+$/)?.[0]??0)))
    for (const ref of refs) {
      if (!ref.edge.data?.alias) {
        const alias = `${ref.node.data.kind==='text'?'文本':'图'}${++sequence}`
        edges[edges.indexOf(ref.edge)] = { ...ref.edge, data: { ...ref.edge.data, alias } }
        changed = true
      }
    }
    if (sequence > Number(node.data.reference_sequence ?? 0)) {
      changed = true
      return { ...node, data: { ...node.data, reference_sequence: sequence } }
    }
    return node
  })
  return changed ? { ...canvas, edges, nodes } : canvas
}

export function videoMode(data: CardData, count: number) {
  return data.mode ?? (count === 0 ? '文生视频' : count === 1 ? '首帧' : '首尾帧')
}

export function referenceIssue(data: CardData, refs: ReturnType<typeof referencesFor>): string | null {
  const images = refs.filter(r => r.node.data.kind !== 'text')
  for (const prompt of [data.prompt,...refs.filter(r=>r.node.data.kind==='text').map(r=>r.node.data.prompt)]) {
    for (const match of prompt.replaceAll('@@','').matchAll(/@((?:图|文本)[1-9]\d*)/g)) {
      if (!refs.some(r=>r.alias===match[1])) return `引用 @${match[1]} 已断开，请重新插入或删除这段引用`
    }
  }
  if (data.kind === 'video') {
    const mode = videoMode(data, images.length)
    const counts: Record<string, number> = { '文生视频': 0, '首帧': 1, '首尾帧': 2, '首尾循环': 1 }
    if (mode === '参考图') {
      if (images.length < 1 || images.length > 9) return '参考图模式需要 1–9 张图片'
    } else if (!(mode in counts)) return '请选择有效的视频模式'
    else if (images.length !== counts[mode]) return `${mode}需要 ${counts[mode]} 张图片，当前 ${images.length} 张`
  }
  if (data.kind === 'image') {
    const limit = imageModel(data.model).maxReferences
    if(data.model==='qwen-edit'&&images.length<1) return 'Qwen Edit 需要连接 1–3 张图片'
    if (images.length > limit) return `此模型最多支持 ${limit} 张图片`
    const mode = data.image_mode ?? (images.length ? 'edit' : 'text')
    if ((mode === 'text') !== (images.length === 0)) return '图片模式与连线不匹配，请切换模式或调整连线'
  }
  if (data.kind === 'upscale' && images.length !== 1) return '超分需要一张图片或一段视频'
  if (data.kind !== 'upscale' && !data.prompt.trim() && !refs.some(r => r.node.data.kind === 'text' && r.node.data.prompt.trim())) return '请填写提示词或连接文本节点'
  return null
}

export function setReferenceRole(canvas: CanvasData, id: string, source: string, role: string): CanvasData {
  const original = canvas.edges.find(e => e.target === id && e.source === source)
  const oldRole = String(original?.data?.role ?? 'reference')
  return { ...canvas, edges: canvas.edges.map(e => {
    if (e.target !== id) return e
    if (e.source === source) return { ...e, data: { ...e.data, role } }
    if (role !== 'reference' && e.data?.role === role) return { ...e, data: { ...e.data, role: oldRole === role ? 'reference' : oldRole } }
    return e
  }) }
}

export function mentionAt(value: string, caret: number) {
  const match = value.slice(0, caret).match(/(?<!@)@([^@\s，。,;；:：!?！？<>]*)$/u)
  return match ? { start: caret - match[0].length, end: caret, query: match[1] } : null
}

export function insertReference(value: string, start: number, end: number, alias: string) {
  const token = '@' + alias + ' '
  return { value: value.slice(0, start) + token + value.slice(end), caret: start + token.length }
}

export function attachReference(canvas: CanvasData, edge: Edge, source: CreativeNode, target: CreativeNode): CanvasData {
  const normalized = normalizeReferences(canvas)
  const sequence = Number(normalized.nodes.find(n => n.id === target.id)?.data.reference_sequence ?? 0) + 1
  const text = source.data.kind === 'text'
  const count = referencesFor(target.id, normalized).filter(r => r.node.data.kind !== 'text').length + Number(!text)
  let mode = target.data.mode
  if (target.data.kind === 'video' && !text) {
    if (!mode || mode === '文生视频') mode = '首帧'
    else if (mode === '首帧' && count === 2) mode = '首尾帧'
  }
  const role = text || target.data.kind==='video'&&mode === '参考图' ? 'reference' : target.data.kind === 'video' ? (count === 1 ? 'first' : 'last') : count === 1 ? 'base' : 'reference'
  return { ...normalized, edges: [...normalized.edges, { ...edge, data: { ...edge.data, alias: `${text?'文本':'图'}${sequence}`, role } }],
    nodes: normalized.nodes.map(n => n.id === target.id ? { ...n, data: { ...n.data, reference_sequence: sequence, ...(target.data.kind === 'video' ? {mode} : {}), ...(target.data.kind==='image'&&!text&&(!target.data.image_mode||target.data.image_mode==='text')?{image_mode:'edit'}:{}) } } : n) }
}
