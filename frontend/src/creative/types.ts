import type { Edge, Node, Viewport } from '@xyflow/react'

export type Kind = 'image' | 'video' | 'text' | 'upload' | 'upscale' | 'audio' | 'group'
export type GenerationSettings = { prompt: string; model?: string; aspect?: string; resolution?: string;
  quality?: string; duration?: number; fps?: number; audio?: boolean; mode?: string; image_mode?: string; seed?: number;
  reference_inputs?: {node_id:string;alias:string;role:string;label:string;asset_id?:string;text?:string}[];
  resolved_prompt?:string; output_size?:[number,number] }
export type NodeVersion = { id: string; asset_id: string; media_type: string; created_at: number;
  job_id?: string; settings?: GenerationSettings; note?: string }
export type CardData = Record<string, unknown> & {
  kind: Kind; label: string; prompt: string; model?: string; aspect?: string; resolution?: string;
  quality?: string; duration?: number; fps?: number; audio?: boolean; mode?: string; image_mode?: string; seed?: number;
  reference_sequence?: number; width?: number; height?: number;
  asset_id?: string; media_type?: string; job_id?: string; pinned?: boolean;
  versions?: NodeVersion[]; active_version_id?: string; version_selection?: 'auto' | 'manual';
}
export type CreativeNode = Node<CardData, 'creative'>
export type ProductionEntity = { id: string; kind: 'character' | 'location' | 'prop'; name: string; description: string }
export type ProductionScene = { id: string; name: string; location_id: string; axis: string; lighting: string }
export type ProductionLock = { id: string; entity_id: string; label: string; asset_id: string; node_id: string;
  continuity: Record<string, unknown>; entity_snapshot?: ProductionEntity; created_at: number }
export type ProductionShot = { id: string; node_id: string; image_node_id: string; end_image_node_id?: string;
  scene_id: string; duration: number; source_duration?: number; start?: number; transition?: string;
  purpose: string; action: string; entities: string[]; bindings: string[];
  camera: { size: string; lens_mm: number; movement: string; motivation: string; axis: string; eyeline: string };
  state_in: Record<string, unknown>; state_out: Record<string, unknown>;
  dialogue?: { speaker: string; text: string }[] }
export type Production = { schema_version?: number; revision: number;
  brief: { title: string; target_seconds: number; conflict: string; arc: string };
  entities: ProductionEntity[]; scenes: ProductionScene[]; shots: ProductionShot[]; locks: ProductionLock[] }
export type CanvasData = { id: string; name: string; nodes: CreativeNode[]; edges: Edge[]; viewport: Viewport; production?: Production }
export type Project = { id: string; name: string; kind: 'project' | 'folder'; folder_id: string | null;
  revision: number; created_at: number; updated_at: number; deleted_at: number | null; canvases: CanvasData[] }
export type ProjectSummary = Omit<Project, 'canvases'> & { canvas_count: number; thumbnail_asset?: string; thumbnail_type?:string }
export type Asset = { id:string; name:string; media_type:string; width?:number; height?:number; duration?:number; created_at:number }
export const names: Record<Kind, string> = { image: '图片', video: '视频', text: '文本', upload: '上传素材', upscale: '超分', audio: '音频', group: '分组' }
export const uid = () => crypto.randomUUID()
export const assetUrl = (id: string) => '/api/assets/' + encodeURIComponent(id) + '/file'
let csrf = ''
export function setToken(token: string) { csrf = token }
export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) { super(message); this.name = 'ApiError'; this.status = status }
}
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, { method, headers: { 'Content-Type': 'application/json', 'X-Workbench-Token': csrf }, body: body === undefined ? undefined : JSON.stringify(body) })
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw new ApiError(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入', response.status) }
  return response.json()
}
export async function upload(file: File) {
  const response = await fetch('/api/assets', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Workbench-Token': csrf, 'X-Filename': encodeURIComponent(file.name) }, body: file })
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '上传失败')
  return data as Asset
}
export function newNode(kind: Kind, position: { x: number; y: number }): CreativeNode {
  return { id: uid(), type: 'creative', position, data: { kind, label: names[kind], prompt: '',
    model: kind === 'image' ? 'minimax-image' : kind === 'video' ? 'minimax-h3' : kind === 'upscale' ? 'seedvr2-7b' : undefined,
    aspect: 'source', resolution: kind === 'video' ? '768p' : kind === 'upscale' ? '2×' : '2K',
    quality: '标准', duration: 5, fps: 24, audio: false, ...(kind==='video'?{mode:'文生视频'}:{}) } }
}
export function cleanCanvas(canvas: CanvasData): CanvasData {
  return { ...canvas, nodes: canvas.nodes.map(n => ({ id: n.id, type: 'creative', position: n.position, data: n.data,
    ...(n.data.kind==='group'?{style:{width:n.width??n.style?.width,height:n.height??n.style?.height},zIndex:-1}:{}),
    ...(n.parentId ? { parentId: n.parentId, extent: n.extent } : {}) })),
    edges: canvas.edges.map(({ id, source, target, sourceHandle, targetHandle, data }) => ({ id, source, target, sourceHandle, targetHandle, data })) }
}
