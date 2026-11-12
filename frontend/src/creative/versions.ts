import type { Job } from '../types'
import type { CardData, NodeVersion, Project } from './types'

export function versionsOf(data: CardData): NodeVersion[] {
  const versions = data.versions ?? []
  if (!data.asset_id || versions.some(v => v.asset_id === data.asset_id)) return versions
  return [...versions, { id: data.asset_id, asset_id: data.asset_id, media_type: data.media_type ?? 'image/png',
    job_id: data.job_id, created_at: 0 }]
}

export function selectVersion(data: CardData, version: NodeVersion): CardData {
  return { ...data, versions: versionsOf(data), active_version_id: version.id,
    asset_id: version.asset_id, media_type: version.media_type, job_id: version.job_id }
}

export function mergeCompleted(project: Project, jobs: Job[]): Project {
  const completed = jobs.filter(j => j.status === 'completed' && j.creative?.project_id === project.id)
    .sort((a, b) => a.created_at - b.created_at)
  let changed = false
  const canvases = project.canvases.map(c => ({ ...c, nodes: c.nodes.map(node => {
    let data = node.data
    const versions = [...versionsOf(data)]
    let added: NodeVersion | undefined
    let enriched = false
    for (const job of completed) {
      if (job.creative?.canvas_id !== c.id) continue
      const output = job.outputs.find(o => o.creative_node === node.id && o.asset_id)
      if (!output?.asset_id) continue
      const settings = job.creative?.node_settings?.[node.id]
      const existing = versions.findIndex(v => v.asset_id === output.asset_id)
      if (existing >= 0) {
        if (!versions[existing].created_at || (!versions[existing].settings && settings)) {
          versions[existing] = { ...versions[existing], created_at: job.created_at, job_id: job.id,
            ...(settings ? { settings: structuredClone(settings) } : {}) }
          enriched = true
        }
        continue
      }
      added = { id: output.asset_id, asset_id: output.asset_id, media_type: output.media_type ?? 'image/png',
        job_id: job.id, created_at: job.created_at, ...(settings ? { settings: structuredClone(settings) } : {}) }
      versions.push(added)
    }
    // WARN: Polling must only activate unseen results, preserving the user's selected output version.
    if (added || enriched) {
      changed = true
      versions.sort((a, b) => a.created_at - b.created_at)
      data = { ...data, versions }
      const active = versions.find(v => v.asset_id === data.asset_id)
      if (added && added.created_at >= (active?.created_at ?? 0)) data = selectVersion(data, added)
    }
    return data === node.data ? node : { ...node, data }
  }) }))
  return changed ? { ...project, canvases } : project
}
