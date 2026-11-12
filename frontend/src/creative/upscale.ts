import type { Edge } from '@xyflow/react'
import type { CreativeNode } from './types'

export const videoUpscalers = [
  { value: 'seedvr2-7b', label: 'SeedVR2 7B · 自然', hint: '自然还原，推荐用于人物和写实视频。' },
  { value: 'seedvr2-7b-sharp', label: 'SeedVR2 7B Sharp · 细节', hint: '增强纹理和线条，适合动画及细节丰富的画面。' },
  { value: 'seedvr2-3b', label: 'SeedVR2 3B · 均衡', hint: '更快、更省显存，兼顾清晰度与处理速度。' },
]
export function videoModel(model?: string) {
  return videoUpscalers.find(p => p.value === model) ?? videoUpscalers[0]
}
export function outputKind(id: string, nodes: CreativeNode[], edges: Edge[], visited = new Set<string>()): 'image' | 'video' | 'audio' | 'text' | 'unknown' {
  const node = nodes.find(n => n.id === id)
  if (!node || visited.has(id)) return 'unknown'
  visited.add(id)
  if (node.data.asset_id && node.data.media_type) return node.data.media_type.split('/')[0] as 'image' | 'video' | 'audio'
  if (node.data.kind === 'upscale') {
    const source = edges.find(e => e.target === id)?.source
    return source ? outputKind(source, nodes, edges, visited) : 'unknown'
  }
  return ['image', 'video', 'audio', 'text'].includes(node.data.kind) ? node.data.kind as 'image' | 'video' | 'audio' | 'text' : 'unknown'
}
export function upscaleInputIsVideo(id: string, nodes: CreativeNode[], edges: Edge[]) {
  return edges.some(e => e.target === id && outputKind(e.source, nodes, edges) === 'video')
}
