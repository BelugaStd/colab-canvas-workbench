import test from 'node:test'
import assert from 'node:assert/strict'
import { mergeCompleted, selectVersion, versionsOf } from '../src/creative/versions.ts'

const project = () => ({id:'p',canvases:[{id:'c',nodes:[{id:'n',data:{kind:'image',label:'Image',prompt:'current draft'}}]}]})
const job = (id, time, prompt) => ({id,created_at:time,status:'completed',creative:{project_id:'p',canvas_id:'c',node_settings:{n:{prompt,model:'moss-image'}}},outputs:[{creative_node:'n',asset_id:id,media_type:'image/png'}]})
const data = p => p.canvases[0].nodes[0].data

test('regenerations accumulate immutable settings without replacing the editing draft', () => {
  const jobs=[job('a',1,'first'),job('b',2,'second')]
  const next=mergeCompleted(project(),jobs)
  assert.equal(data(next).prompt,'current draft')
  assert.deepEqual(data(next).versions.map(v=>v.asset_id),['a','b'])
  assert.equal(data(next).asset_id,'b')
  jobs[0].creative.node_settings.n.prompt='mutated'
  assert.equal(data(next).versions[0].settings.prompt,'first')
})
test('manual output survives polling, save/reload and newly completed results', () => {
  const jobs=[job('a',1,'first'),job('b',2,'second')]
  let next=mergeCompleted(project(),jobs)
  next.canvases[0].nodes[0].data=selectVersion(data(next),data(next).versions[0])
  next=JSON.parse(JSON.stringify(next))
  assert.equal(mergeCompleted(next,jobs),next)
  assert.equal(data(next).asset_id,'a')
  next=mergeCompleted(next,[...jobs,job('c',3,'third')])
  assert.equal(data(next).asset_id,'a')
  assert.equal(data(next).versions.length,3)
  assert.equal(data(next).version_selection,'manual')
  data(next).version_selection='auto'
  next=mergeCompleted(next,[...jobs,job('c',3,'third'),job('d',4,'explicit regeneration')])
  assert.equal(data(next).asset_id,'d')
  assert.equal(data(next).version_selection,'auto')
})
test('legacy assets are retained and matching historical jobs enrich without duplicating', () => {
  const p=project();Object.assign(data(p),{asset_id:'a',media_type:'image/png'})
  const next=mergeCompleted(p,[job('a',1,'first')])
  assert.equal(versionsOf(data(next)).length,1)
  assert.equal(data(next).versions[0].settings.prompt,'first')
  assert.equal(data(next).asset_id,'a')
})
test('other canvases, projects, and unsuccessful jobs do not create versions', () => {
  const p=project(), jobs=[job('a',1,'first'),job('b',2,'second'),job('c',3,'third')]
  jobs[0].creative.project_id='other';jobs[1].creative.canvas_id='other';jobs[2].status='running'
  assert.equal(mergeCompleted(p,jobs),p)
})
test('migrating multiple historical runs keeps the latest existing output and chronological versions', () => {
  const p=project();Object.assign(data(p),{asset_id:'b',media_type:'image/png'})
  const next=mergeCompleted(p,[job('a',1,'first'),job('b',2,'second')])
  assert.equal(data(next).asset_id,'b')
  assert.deepEqual(data(next).versions.map(v=>v.id),['a','b'])
})
test('replacing an uploaded asset retains both original and replacement', () => {
  const d={kind:'upload',label:'ref',prompt:'',asset_id:'new',versions:[{id:'old',asset_id:'old',created_at:0,media_type:'image/png'}]}
  assert.deepEqual(versionsOf(d).map(v=>v.id),['old','new'])
})
