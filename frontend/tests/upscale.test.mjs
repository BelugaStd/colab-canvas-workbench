import test from 'node:test'
import assert from 'node:assert/strict'
import { outputKind, upscaleInputIsVideo, videoModel, videoUpscalers } from '../src/creative/upscale.ts'

const nodes = [{id:'v',data:{kind:'video'}},{id:'a',data:{kind:'upscale'}},{id:'b',data:{kind:'upscale'}}]
const edges = [{source:'v',target:'a'},{source:'a',target:'b'}]
test('video identity propagates through ungenerated upscale chains', () => {
  assert.equal(outputKind('b',nodes,edges),'video')
  assert.equal(upscaleInputIsVideo('b',nodes,edges),true)
})
test('selected output determines downstream media while editor uses its input', () => {
  const changed = structuredClone(nodes)
  Object.assign(changed[1].data,{asset_id:'old',media_type:'image/png'})
  assert.equal(upscaleInputIsVideo('a',changed,edges),true)
  assert.equal(upscaleInputIsVideo('b',changed,edges),false)
})
test('cycles stop and three distinct video models remain selectable', () => {
  assert.equal(outputKind('a',nodes,[{source:'a',target:'b'},{source:'b',target:'a'}]),'unknown')
  assert.equal(videoUpscalers.length,3)
  assert.equal(new Set(videoUpscalers.map(m=>m.value)).size,3)
  assert.equal(videoModel('realesrgan').value,'seedvr2-7b')
})
