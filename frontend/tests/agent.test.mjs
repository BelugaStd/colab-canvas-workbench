import test from 'node:test'
import assert from 'node:assert/strict'
import { agentBusyStatuses, agentTurns, cachePercentage, insertAgentMention, toolText, updateSessionRun } from '../src/creative/agentTypes.ts'

const run = (id, status = 'running') => ({ id, session_id: 'session', status, events: [], usage: { input_tokens: 10, output_tokens: 2, cached_tokens: null } })
const session = { id: 'session', messages: [], runs: [], active_run_id: null }

test('cache statistics distinguish unreported cache from a real zero hit', () => {
  assert.equal(cachePercentage({ input_tokens: 1200, output_tokens: 20, cached_tokens: null }), null)
  assert.equal(cachePercentage({ input_tokens: 1200, output_tokens: 20, cached_tokens: 0 }), 0)
  assert.equal(cachePercentage({ input_tokens: 1200, output_tokens: 20, cached_tokens: 900 }), 75)
  assert.equal(cachePercentage({ input_tokens: 0, output_tokens: 20, cached_tokens: 0 }), null)
  assert.equal(cachePercentage({ input_tokens: 1200, output_tokens: 20, cached_tokens: -1 }), null)
})

test('node mention replaces a partial at the caret while preserving the suffix', () => {
  const value = '以 @机 为首帧，向前推进', caret = 4
  const result = insertAgentMention(value, caret, caret, { id: 'character-1' })
  assert.equal(result.value, '以 @[node:character-1]  为首帧，向前推进')
  assert.equal(result.value.slice(result.caret), ' 为首帧，向前推进')
  assert.equal(insertAgentMention('前 后', 2, 2, { id: 'image-2' }).value, '前 @[node:image-2] 后')
})

test('node mentions use identity rather than a mutable or duplicate display label', () => {
  const first = insertAgentMention('', 0, 0, { id: 'image-a', data: { label: '首帧' } })
  const second = insertAgentMention('', 0, 0, { id: 'image-b', data: { label: '首帧' } })
  assert.equal(first.value, '@[node:image-a] ')
  assert.notEqual(first.value, second.value)
})

test('polling a run updates it in place without adding duplicate executions', () => {
  const running = updateSessionRun(session, run('a'))
  assert.equal(running.active_run_id, 'a')
  const done = updateSessionRun(running, run('a', 'completed'))
  assert.equal(done.runs.length, 1)
  assert.equal(done.runs[0].status, 'completed')
  assert.equal(done.active_run_id, null)
  assert.ok(agentBusyStatuses.has('awaiting_approval'))
  assert.ok(!agentBusyStatuses.has('interrupted'))
})

test('saved turns retain prior tool history while replacing the current run snapshot', () => {
  const saved = { ...session, messages: [
    { role: 'user', content: '策划分镜' }, { role: 'assistant', content: '六个镜头' },
    { role: 'user', content: '开始生成' },
  ], runs: [run('a', 'completed'), run('b')] }
  const turns = agentTurns(saved, run('b', 'completed'))
  assert.equal(turns.length, 2)
  assert.equal(turns[0].assistants[0].content, '六个镜头')
  assert.equal(turns[0].run.id, 'a')
  assert.equal(turns[1].run.id, 'b')
  assert.equal(turns[1].run.status, 'completed')
  assert.equal(saved.runs[1].status, 'running')
})

test('tool detail output hides sensitive named fields without altering useful output', () => {
  const text = toolText({ api_key: 'private', nested: { token: 'hidden', asset_id: 'visible' } })
  assert.ok(!text.includes('private'))
  assert.ok(!text.includes('hidden'))
  assert.ok(text.includes('visible'))
  assert.equal(toolText(null), '')
})
