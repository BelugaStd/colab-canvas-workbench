import test from 'node:test'
import assert from 'node:assert/strict'
import { attachReference, insertReference, mentionAt, normalizeReferences, referenceIssue, referencesFor, setReferenceRole } from '../src/creative/references.ts'

const node=(id,kind,data={})=>({id,data:{kind,prompt:'test',...data}})
test('deleted aliases never silently point to newly connected images',()=>{
  let canvas={nodes:[node('a','upload'),node('b','upload'),node('v','video',{mode:'文生视频'})],edges:[]}
  canvas=attachReference(canvas,{source:'a',target:'v'},canvas.nodes[0],canvas.nodes[2])
  assert.equal(canvas.nodes[2].data.mode,'首帧')
  assert.equal(referencesFor('v',canvas)[0].alias,'图1')
  canvas.edges=[]
  canvas=attachReference(canvas,{source:'b',target:'v'},canvas.nodes[1],canvas.nodes[2])
  assert.equal(referencesFor('v',canvas)[0].alias,'图2')
})
test('roles swap atomically and renames do not change mentions',()=>{
  let canvas={nodes:[node('a','upload'),node('b','upload'),node('v','video')],edges:[{source:'a',target:'v',data:{alias:'图4',role:'first'}},{source:'b',target:'v',data:{alias:'图7',role:'last'}}]}
  canvas=setReferenceRole(canvas,'v','b','first')
  assert.deepEqual(canvas.edges.map(e=>e.data.role),['last','first'])
  canvas.nodes[0].data.label='renamed'
  assert.deepEqual(referencesFor('v',canvas).map(r=>r.alias),['图4','图7'])
  assert.equal(normalizeReferences(canvas).nodes[2].data.reference_sequence,7)
})
test('mention inserts at the caret without destroying the prompt suffix',()=>{
  const value='Start @图 then continue', caret=8
  const match=mentionAt(value,caret)
  assert.equal(match.query,'图')
  assert.deepEqual(insertReference(value,match.start,match.end,'图12'),{value:'Start @图12  then continue',caret:11})
  assert.equal(mentionAt('literal @@',10),null)
})
test('reference mode still requires a prompt or a connected nonempty text',()=>{
  const image={node:node('a','upload')}, text={node:node('t','text',{prompt:'Describe motion'})}
  assert.ok(referenceIssue(node('v','video',{prompt:'',mode:'参考图'}).data,[image]))
  assert.equal(referenceIssue(node('v','video',{prompt:'',mode:'参考图'}).data,[image,text]),null)
})
test('explicit modes reject missing and extra input instead of changing generation type',()=>{
  assert.match(referenceIssue(node('v','video',{mode:'首帧'}).data,[]),/1/)
  assert.match(referenceIssue(node('v','video',{mode:'文生视频'}).data,[{node:node('a','upload')}]),/0/)
  assert.ok(referenceIssue(node('i','image',{image_mode:'edit'}).data,[]))
})
test('partial legacy migration cannot assign an already used alias',()=>{
  const canvas={nodes:[node('a','upload'),node('b','upload'),node('v','video')],edges:[{source:'a',target:'v',data:{alias:'图2'}},{source:'b',target:'v'}]}
  const refs=referencesFor('v',normalizeReferences(canvas))
  assert.equal(new Set(refs.map(r=>r.alias)).size,2)
})
