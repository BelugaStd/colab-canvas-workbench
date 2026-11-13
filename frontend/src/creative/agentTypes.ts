import type { CreativeNode } from './types'

export type AgentProtocol = 'responses' | 'chat_completions'
export type AgentMode = 'plan' | 'auto'
export type AgentSettings = {
  base_url: string; model: string; protocol: AgentProtocol; has_key: boolean; configured: boolean;
  timeout_seconds: number; max_output_tokens: number; strict_tools: boolean;
  cache_key?: string; cache_options?: Record<string, unknown>;
}
export type AgentUsage = { input_tokens: number; output_tokens: number; cached_tokens: number | null }
export type AgentRunStatus = 'running' | 'waiting' | 'awaiting_approval' | 'completed' | 'failed' | 'cancelled' | 'interrupted'
export type AgentEvent = {
  id: string; type: 'tool' | 'message' | 'plan'; name?: string;
  status: 'running' | 'completed' | 'failed' | 'approval'; arguments?: unknown; result?: unknown;
  text?: string; created_at: number;
}
export type AgentRun = {
  id: string; session_id: string; status: AgentRunStatus; message: string; events: AgentEvent[]; mode?: AgentMode;
  usage: AgentUsage; error?: string; project_revision?: number;
  approval?: { call_id: string; name: string; arguments: unknown };
  created_at: number; updated_at: number;
}
export type AgentMessage = { role: 'user' | 'assistant'; content: string }
export type AgentSessionSummary = { id: string; title: string; status: string; updated_at: number; canvas_id: string }
export type AgentSession = AgentSessionSummary & {
  project_id: string; messages: AgentMessage[]; runs: AgentRun[]; active_run_id?: string | null;
}

export const agentBusyStatuses = new Set<AgentRunStatus>(['running', 'waiting', 'awaiting_approval'])
export const agentRunLabels: Record<AgentRunStatus, string> = {
  running: '正在创作', waiting: '等待生成', awaiting_approval: '等待确认', completed: '已完成',
  failed: '执行失败', cancelled: '已取消', interrupted: '已中断',
}

export function cachePercentage(usage?: AgentUsage): number | null {
  if (!usage || usage.cached_tokens == null || !Number.isFinite(usage.cached_tokens)
    || !Number.isFinite(usage.input_tokens) || usage.input_tokens <= 0 || usage.cached_tokens < 0) return null
  return Math.min(100, 100 * usage.cached_tokens / usage.input_tokens)
}

export function insertAgentMention(value: string, start: number, end: number, node: Pick<CreativeNode, 'id'>) {
  const before = value.slice(0, start), typed = before.match(/@[^\s@]*$/u)
  const from = typed ? start - typed[0].length : start
  const mention = `@[node:${node.id}] `
  return { value: value.slice(0, from) + mention + value.slice(end), caret: from + mention.length }
}

export function updateSessionRun(session: AgentSession, run: AgentRun): AgentSession {
  const exists = session.runs.some(item => item.id === run.id)
  return { ...session, runs: exists ? session.runs.map(item => item.id === run.id ? run : item) : [...session.runs, run],
    active_run_id: agentBusyStatuses.has(run.status) ? run.id : null }
}

export function agentTurns(session: AgentSession | null, live?: AgentRun | null) {
  if (!session) return []
  const current = live && live.session_id === session.id ? updateSessionRun(session, live) : session
  const turns: { user?: AgentMessage; assistants: AgentMessage[]; run?: AgentRun }[] = []
  let runIndex = 0
  for (const message of current.messages) {
    if (message.role === 'user') turns.push({ user: message, assistants: [], run: current.runs[runIndex++] })
    else {
      if (!turns.length) turns.push({ assistants: [] })
      turns.at(-1)!.assistants.push(message)
    }
  }
  for (const run of current.runs.slice(runIndex)) turns.push({ assistants: [], run })
  return turns
}

export function toolText(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string') return value.slice(0, 24000)
  const text = JSON.stringify(value, (key, item) => /^(api_key|authorization|token|secret|password)$/i.test(key) ? '[已隐藏]' : item, 2)
  return (text ?? '').slice(0, 24000)
}
