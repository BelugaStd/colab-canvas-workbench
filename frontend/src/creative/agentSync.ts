import type { Project } from './types'

type Item = { id: string; [key: string]: unknown }
const equal = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right)
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const idArray = (value: unknown): value is Item[] => Array.isArray(value) && value.every(item => record(item) && typeof item.id === 'string')

export function mergeAgentProject(base: Project, local: Project, remote: Project): { project: Project; conflicts: string[] } {
  if (base.id !== local.id || local.id !== remote.id) throw new Error('不能合并不同项目')
  const conflicts: string[] = []
  function merge(before: unknown, ours: unknown, theirs: unknown, path: string): unknown {
    if (equal(ours, theirs)) return ours
    if (equal(ours, before)) return theirs
    if (equal(theirs, before)) return ours
    if ((before === undefined || idArray(before)) && idArray(ours) && idArray(theirs)) {
      const previous = new Map((before ?? []).map(item => [item.id, item])), left = new Map(ours.map(item => [item.id, item])), right = new Map(theirs.map(item => [item.id, item]))
      const ids = [...new Set([...theirs.map(item => item.id), ...ours.map(item => item.id)])]
      return ids.flatMap(id => {
        const value = merge(previous.get(id), left.get(id), right.get(id), `${path}[${id}]`)
        return value === undefined ? [] : [value]
      })
    }
    if (record(ours) && record(theirs) && (before === undefined || record(before))) {
      const previous = record(before) ? before : {}
      const result = Object.fromEntries([...new Set([...Object.keys(previous), ...Object.keys(ours), ...Object.keys(theirs)])].flatMap(key => {
        const value = merge(previous[key], ours[key], theirs[key], path ? `${path}.${key}` : key)
        return value === undefined ? [] : [[key, value]]
      }))
      if (path.endsWith('.data') && ours.version_selection === 'manual' && theirs.version_selection !== 'manual') {
        for (const key of ['asset_id', 'active_version_id', 'media_type', 'job_id']) {
          if (ours[key] === undefined) delete result[key]
          else result[key] = ours[key]
        }
      }
      return result
    }
    conflicts.push(path)
    return ours
  }
  const project: Project = { ...remote,
    name: merge(base.name, local.name, remote.name, 'name') as string,
    folder_id: merge(base.folder_id, local.folder_id, remote.folder_id, 'folder_id') as string | null,
    canvases: merge(base.canvases, local.canvases, remote.canvases, 'canvases') as Project['canvases'],
  }
  project.canvases = project.canvases.map(canvas => {
    const ids = new Set(canvas.nodes.map(node => node.id))
    return { ...canvas, edges: canvas.edges.filter(edge => {
      if (ids.has(edge.source) && ids.has(edge.target)) return true
      conflicts.push(`canvases[${canvas.id}].edges[${edge.id}]`)
      return false
    }) }
  })
  return { project, conflicts }
}

export function preserveCanvasSelection(local: Project, incoming: Project): Project {
  return { ...incoming, canvases: incoming.canvases.map(canvas => {
    const previous = local.canvases.find(item => item.id === canvas.id)
    const nodes = new Map(previous?.nodes.map(node => [node.id, node]))
    const edges = new Map(previous?.edges.map(edge => [edge.id, edge]))
    // WARN: Losing measured bounds remounts React Flow's selection overlay and steals focus during composition.
    return { ...canvas, nodes: canvas.nodes.map(node => ({ ...node, selected: nodes.get(node.id)?.selected ?? false,
      ...(nodes.get(node.id)?.measured ? { measured: nodes.get(node.id)!.measured } : {}) })),
      edges: canvas.edges.map(edge => ({ ...edge, selected: edges.get(edge.id)?.selected ?? false })) }
  }) }
}
