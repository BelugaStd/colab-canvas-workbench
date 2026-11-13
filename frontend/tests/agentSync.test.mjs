import test from 'node:test'
import assert from 'node:assert/strict'
import { mergeAgentProject, preserveCanvasSelection } from '../src/creative/agentSync.ts'

const node = (id, prompt = 'original') => ({ id, type: 'creative', position: { x: 0, y: 0 }, data: { kind: 'image', label: id, prompt } })
const project = () => ({ id: 'p', revision: 1, name: 'Story', folder_id: null, canvases: [{ id: 'c', name: 'Canvas', viewport: { x: 0, y: 0, zoom: 1 }, nodes: [node('a')], edges: [] }] })
const clone = value => structuredClone(value)

test('manual prompt and position survive Agent adding a connected video node', () => {
  const base = project(), local = clone(base), remote = clone(base)
  local.canvases[0].nodes[0].data.prompt = 'my edit'
  local.canvases[0].nodes[0].position.x = 420
  remote.revision = 2
  remote.canvases[0].nodes.push({ ...node('v'), data: { kind: 'video', label: 'Shot', prompt: 'slow dolly' } })
  remote.canvases[0].edges.push({ id: 'e', source: 'a', target: 'v' })
  const result = mergeAgentProject(base, local, remote)
  assert.equal(result.project.revision, 2)
  assert.equal(result.project.canvases[0].nodes[0].data.prompt, 'my edit')
  assert.equal(result.project.canvases[0].nodes[0].position.x, 420)
  assert.equal(result.project.canvases[0].nodes.length, 2)
  assert.equal(result.project.canvases[0].edges.length, 1)
  assert.deepEqual(result.conflicts, [])
})

test('concurrent same-field edits are visible and keep the local draft', () => {
  const base = project(), local = clone(base), remote = clone(base)
  local.canvases[0].nodes[0].data.prompt = 'manual'
  remote.canvases[0].nodes[0].data.prompt = 'agent'
  const result = mergeAgentProject(base, local, remote)
  assert.equal(result.project.canvases[0].nodes[0].data.prompt, 'manual')
  assert.deepEqual(result.conflicts, ['canvases[c].nodes[a].data.prompt'])
})

test('independent deletions are kept and newly dangling links are reported and removed', () => {
  const base = project(), local = clone(base), remote = clone(base)
  local.canvases[0].nodes = []
  remote.canvases[0].nodes.push(node('v'))
  remote.canvases[0].edges.push({ id: 'e', source: 'a', target: 'v' })
  const result = mergeAgentProject(base, local, remote)
  assert.deepEqual(result.project.canvases[0].nodes.map(item => item.id), ['v'])
  assert.deepEqual(result.project.canvases[0].edges, [])
  assert.deepEqual(result.conflicts, ['canvases[c].edges[e]'])
})

test('an edit made after a submitted save survives acknowledgement and subsequent Agent merge', () => {
  const submitted = project(), local = clone(submitted), acknowledgement = clone(submitted)
  acknowledgement.revision = 2
  local.canvases[0].nodes[0].data.prompt = 'typed during save'
  const saved = mergeAgentProject(submitted, local, acknowledgement).project
  const remote = clone(acknowledgement)
  remote.revision = 3
  remote.canvases[0].nodes.push(node('agent'))
  const next = mergeAgentProject(acknowledgement, saved, remote).project
  assert.equal(next.canvases[0].nodes[0].data.prompt, 'typed during save')
  assert.equal(next.canvases[0].nodes.length, 2)
})

test('version collections merge by ID and manual output survives a new remote output', () => {
  const base = project(), local = clone(base), remote = clone(base)
  base.canvases[0].nodes[0].data.versions = [{ id: 'v1', asset_id: 'one' }]
  Object.assign(local.canvases[0].nodes[0].data, { versions: [...base.canvases[0].nodes[0].data.versions, { id: 'v2', asset_id: 'two' }], version_selection: 'manual', asset_id: 'one' })
  Object.assign(remote.canvases[0].nodes[0].data, { versions: [...base.canvases[0].nodes[0].data.versions, { id: 'v3', asset_id: 'three' }], version_selection: 'auto', asset_id: 'three' })
  const result = mergeAgentProject(base, local, remote)
  assert.equal(result.project.canvases[0].nodes[0].data.versions.length, 3)
  assert.equal(result.project.canvases[0].nodes[0].data.asset_id, 'one')
  assert.equal(result.project.canvases[0].nodes[0].data.version_selection, 'manual')
  assert.equal(result.conflicts.length, 2)
})

test('transient selection stays local, including when the remote removes a node', () => {
  const local = project(), remote = clone(local)
  local.canvases[0].nodes[0].selected = true
  local.canvases[0].nodes[0].measured = { width: 300, height: 420 }
  remote.canvases[0].nodes.push(node('new'))
  const result = preserveCanvasSelection(local, remote)
  assert.equal(result.canvases[0].nodes[0].selected, true)
  assert.deepEqual(result.canvases[0].nodes[0].measured, { width: 300, height: 420 })
  assert.equal(result.canvases[0].nodes[1].selected, false)
  assert.equal(remote.canvases[0].nodes[0].selected, undefined)
  remote.canvases[0].nodes = []
  assert.equal(preserveCanvasSelection(local, remote).canvases[0].nodes.length, 0)
})

test('manual output is atomic even when only the selection marker changed locally', () => {
  const base = project()
  Object.assign(base.canvases[0].nodes[0].data, { asset_id: 'old', active_version_id: 'old', media_type: 'image/png', job_id: 'job1', version_selection: 'auto' })
  const local = clone(base), remote = clone(base)
  local.canvases[0].nodes[0].data.version_selection = 'manual'
  Object.assign(remote.canvases[0].nodes[0].data, { asset_id: 'new', active_version_id: 'new', media_type: 'image/webp', job_id: 'job2' })
  const result = mergeAgentProject(base, local, remote).project.canvases[0].nodes[0].data
  assert.deepEqual([result.version_selection, result.asset_id, result.active_version_id, result.media_type, result.job_id], ['manual', 'old', 'old', 'image/png', 'job1'])
})

test('the first concurrently created version lists merge without dropping either output', () => {
  const base = project(), local = clone(base), remote = clone(base)
  local.canvases[0].nodes[0].data.versions = [{ id: 'a', asset_id: 'one' }]
  remote.canvases[0].nodes[0].data.versions = [{ id: 'b', asset_id: 'two' }]
  const result = mergeAgentProject(base, local, remote)
  assert.deepEqual(new Set(result.project.canvases[0].nodes[0].data.versions.map(version => version.id)), new Set(['a', 'b']))
  assert.deepEqual(result.conflicts, [])
})

test('projects with different identities cannot be merged', () => {
  const base = project(), local = clone(base), remote = clone(base)
  remote.id = 'other'
  assert.throws(() => mergeAgentProject(base, local, remote), /不能合并不同项目/)
})
