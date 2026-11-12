import type { Edge, Node, Viewport } from '@xyflow/react'

export type Kind = 'image' | 'video' | 'text' | 'upload' | 'upscale' | 'audio' | 'group'
export type GenerationSettings = { prompt: string; model?: string; aspect?: string; resolution?: string;
  quality?: string; duration?: number; fps?: number; audio?: boolean; mode?: string }
export type NodeVersion = { id: string; asset_id: string; media_type: string; created_at: number;
  job_id?: string; settings?: GenerationSettings; note?: string }
export type CardData = Record<string, unknown> & {
  kind: Kind; label: string; prompt: string; model?: string; aspect?: string; resolution?: string;
  quality?: string; duration?: number; fps?: number; audio?: boolean; mode?: string;
  asset_id?: string; media_type?: string; job_id?: string; pinned?: boolean;
  versions?: NodeVersion[]; active_version_id?: string;
}
export type CreativeNode = Node<CardData, 'creative'>
export type CanvasData = { id: string; name: string; nodes: CreativeNode[]; edges: Edge[]; viewport: Viewport }
export type Project = { id: string; name: string; kind: 'project' | 'folder'; folder_id: string | null;
  revision: number; created_at: number; updated_at: number; deleted_at: number | null; canvases: CanvasData[] }
export type ProjectSummary = Omit<Project, 'canvases'> & { canvas_count: number; thumbnail_asset?: string; thumbnail_type?:string }
export type Asset = { id:string; name:string; media_type:string; width?:number; height?:number; duration?:number; created_at:number }
export const names: Record<Kind, string> = { image: '图片', video: '视频', text: '文本', upload: '上传素材', upscale: '超分', audio: '音频', group: '分组' }
export const uid = () => crypto.randomUUID()
export const assetUrl = (id: string) => '/api/assets/' + encodeURIComponent(id) + '/file'
let csrf = ''
export function setToken(token: string) { csrf = token }
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, { method, headers: { 'Content-Type': 'application/json', 'X-Workbench-Token': csrf }, body: body === undefined ? undefined : JSON.stringify(body) })
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入') }
  return response.json()
}
export async function upload(file: File) {
  const response = await fetch('/api/assets', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Workbench-Token': csrf, 'X-Filename': encodeURIComponent(file.name) }, body: file })
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '上传失败')
  return data as { id: string; name: string; media_type: string }
}
export function newNode(kind: Kind, position: { x: number; y: number }): CreativeNode {
  return { id: uid(), type: 'creative', position, data: { kind, label: names[kind], prompt: '',
    model: kind === 'image' ? 'minimax-image' : kind === 'video' ? 'minimax-h3' : 'realesrgan',
    aspect: kind === 'video' ? '16:9' : '1:1', resolution: kind === 'video' ? '768p' : '1K',
    quality: '标准', duration: 5, fps: 24, audio: false, mode: '首尾帧' } }
}
export function cleanCanvas(canvas: CanvasData): CanvasData {
  return { ...canvas, nodes: canvas.nodes.map(n => ({ id: n.id, type: 'creative', position: n.position, data: n.data,
    ...(n.data.kind==='group'?{style:{width:n.width??n.style?.width,height:n.height??n.style?.height},zIndex:-1}:{}),
    ...(n.parentId ? { parentId: n.parentId, extent: n.extent } : {}) })),
    edges: canvas.edges.map(({ id, source, target, sourceHandle, targetHandle, data }) => ({ id, source, target, sourceHandle, targetHandle, data })) }
}
