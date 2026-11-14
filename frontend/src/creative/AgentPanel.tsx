import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { motion } from 'motion/react'
import { ArrowUp, AtSign, Check, ChevronDown, ChevronLeft, ChevronRight, CircleHelp, Clapperboard, FileText,
  History, Image, LoaderCircle, MessageSquare, Paperclip, Plus, RefreshCw, Settings2, Sparkles, Square, Workflow, X } from 'lucide-react'
import { api, assetUrl, names, uid, type CanvasData, type CreativeNode, type Project } from './types'
import { DraftInput, DraftTextarea } from './DraftText'
import GlassSelect from './GlassSelect'
import AgentMarkdown from './AgentMarkdown'
import ProductionLedger from './ProductionLedger'
import { Presence, Reveal, Surface, transition } from './Motion'
import { agentBusyStatuses, agentRunLabels, agentTurns, cachePercentage, insertAgentMention, toolText, updateSessionRun,
  type AgentEvent, type AgentMode, type AgentRun, type AgentSession, type AgentSessionSummary, type AgentSettings, type AgentUsage } from './agentTypes'

type Props = {
  project: Project; canvas: CanvasData; account: string; close: () => void;
  onProjectChanged: (project: Project) => void; onSessionsChanged?: () => void;
  sessionId?: string; onSessionChange?: (id: string) => void;
  beforeSend?: () => Promise<void>; onAttachAsset?: () => Promise<CreativeNode | void>;
}

const toolNames: Record<string, string> = {
  inspect_canvas: '查看画布', inspect_assets: '查看参考素材', save_story: '保存故事分镜', server: '管理算力',
  cancel_job: '取消生成', resume_job: '恢复生成', assemble_film: '合成影片', manage_project: '整理项目',
  inspect_project: '查看画布', get_project: '查看项目', read_project: '查看项目', list_assets: '查找素材',
  edit_canvas: '编排画布', update_canvas: '更新画布', apply_canvas: '编排工作流',
  create_nodes: '创建节点', connect_nodes: '连接节点', layout_canvas: '整理画布',
  generate: '开始生成', generate_nodes: '生成画面', generate_workflow: '生成工作流',
  get_jobs: '查看生成进度', wait_jobs: '等待成片', inspect_media: '检查画面', extract_frame: '提取视频帧',
  compose_video: '合成影片', assemble_video: '合成影片', select_version: '选择输出版本',
  get_server: '查看服务器', get_compute: '查看算力', prepare_models: '准备模型',
  save_production: '保存制作台账', lock_asset_version: '锁定素材版本', review_media: '审查媒体',
  synthesize_speech: '生成对白', mix_sound: '混合声音',
}
const toolLabel = (name?: string) => name ? toolNames[name] ?? name.replaceAll('_', ' ') : '执行工具'
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '连接暂时不可用，请重试'
const tokens = (value?: number | null) => value == null || !Number.isFinite(value) ? '—' : value.toLocaleString()

function CacheUsage({ usage }: { usage?: AgentUsage }) {
  const percentage = cachePercentage(usage)
  return <span className="ca-cache" title={`输入 ${tokens(usage?.input_tokens)} · 输出 ${tokens(usage?.output_tokens)} · 缓存 ${tokens(usage?.cached_tokens)} tokens`}>
    <span className="ca-cache-dot"/>缓存 {percentage == null ? '—' : `${percentage.toFixed(0)}%`}
  </span>
}

function UserMessage({ content }: { content: string }) {
  const [expanded, setExpanded] = useState(false)
  const long = content.length > 500
  return <Surface tone="fade" className="ca-user-message">
    {long ? <><motion.div className={`ca-user-content ${expanded ? '' : 'is-folded'}`} initial={false} animate={{ height: expanded ? 'auto' : 140 }} transition={transition}>{content}</motion.div>
      <button className="ca-user-toggle" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? '收起' : '展开全部'}<ChevronDown size={12} style={{ rotate: expanded ? '180deg' : '0deg' }}/></button></> : content}
  </Surface>
}

function EventCard({ event }: { event: AgentEvent }) {
  const [open, setOpen] = useState(false)
  const busy = event.status === 'running', failed = event.status === 'failed'
  if (event.type === 'message') return event.text ? <Surface tone="fade" className="ca-assistant-text"><AgentMarkdown text={event.text}/></Surface> : null
  if (event.type === 'plan') return <Surface className="ca-plan"><div><Clapperboard size={15}/><strong>创作计划</strong></div><AgentMarkdown text={event.text || toolText(event.result)}/></Surface>
  return <Surface tone="fade" className={`ca-tool ${failed ? 'failed' : ''}`}>
    <button className="ca-tool-heading" onClick={() => setOpen(value => !value)} aria-expanded={open}>
      {busy ? <LoaderCircle size={14} className="ca-spin"/> : failed ? <CircleHelp size={14}/> : <Check size={14}/>}
      <span>{toolLabel(event.name)}</span><small>{busy ? '进行中' : failed ? '需处理' : event.status === 'approval' ? '待确认' : '完成'}</small>
      <ChevronRight size={13} style={{ rotate: open ? '90deg' : '0deg' }}/>
    </button>
    <Presence>{open && <Reveal className="ca-tool-body">
      {!!event.text && <p>{event.text}</p>}
      {event.arguments != null && <><small>输入</small><pre>{toolText(event.arguments)}</pre></>}
      {event.result != null && <><small>结果</small><pre>{toolText(event.result)}</pre></>}
      {event.arguments == null && event.result == null && !event.text && <p>正在等待工具结果…</p>}
    </Reveal>}</Presence>
  </Surface>
}

function RunCard({ run, assistantTexts, action, actionBusy }: {
  run: AgentRun; assistantTexts: string[]; action: (operation: 'cancel' | 'resume' | 'approve' | 'reject') => void; actionBusy: boolean;
}) {
  const busy = agentBusyStatuses.has(run.status)
  const events = run.events.filter(event => event.type !== 'message' || !assistantTexts.includes(event.text ?? ''))
  return <div className="ca-run" data-run-status={run.status}>
    <div className="ca-run-events">{events.map(event => <EventCard key={event.id} event={event}/>)}</div>
    <div className={`ca-run-status ${run.status === 'failed' ? 'failed' : ''}`}>
      {busy && run.status !== 'awaiting_approval' ? <LoaderCircle size={12} className="ca-spin"/> : run.status === 'completed' ? <Check size={12}/> : <span className="ca-status-marker"/>}
      <span>{agentRunLabels[run.status]}</span><CacheUsage usage={run.usage}/>
    </div>
    {run.message && busy && <p className="ca-run-message">{run.message}</p>}
    {run.error && <p className="ca-inline-error" role="alert">{run.error}</p>}
    <Presence>{run.status === 'awaiting_approval' && <Reveal><div className="ca-approval">
      <strong>继续执行{run.approval ? ` · ${toolLabel(run.approval.name)}` : ''}</strong>
      <p>{run.approval ? toolText(run.approval.arguments) : run.message}</p>
      <button className="ca-button-primary" disabled={actionBusy} onClick={() => action('approve')}>确认继续</button>
      <button disabled={actionBusy} onClick={() => action('reject')}>暂不执行</button>
    </div></Reveal>}</Presence>
    {(['interrupted', 'failed', 'cancelled'] as string[]).includes(run.status) && <button className="ca-resume" disabled={actionBusy} onClick={() => action('resume')}><RefreshCw size={13}/>恢复执行</button>}
  </div>
}

function SettingsForm({ close, onSaved }: { close: () => void; onSaved: (settings: AgentSettings) => void }) {
  const [settings, setSettings] = useState<AgentSettings | null>(null)
  const [key, setKey] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState(''), [tested, setTested] = useState('')
  useEffect(() => {
    let alive = true
    api<AgentSettings>('/agent/settings').then(value => { if (alive) setSettings(value) }).catch(reason => { if (alive) setError(errorMessage(reason)) })
    return () => { alive = false }
  }, [])
  const change = (patch: Partial<AgentSettings>) => { setSettings(value => value ? { ...value, ...patch } : value); setTested('') }
  async function save(test: boolean) {
    if (!settings || busy) return
    if (!Number.isInteger(settings.timeout_seconds) || settings.timeout_seconds < 15 || settings.timeout_seconds > 600
      || !Number.isInteger(settings.max_output_tokens) || settings.max_output_tokens < 1024 || settings.max_output_tokens > 64000) {
      setError('超时需为 15–600 秒，最大输出需为 1024–64000 个 tokens，均需为整数'); return
    }
    setBusy(true); setError(''); setTested('')
    try {
      const { has_key: _hasKey, configured: _configured, ...values } = settings
      const saved = await api<AgentSettings>('/agent/settings', 'PUT', { ...values, strict_tools: false, ...(key ? { api_key: key } : {}) })
      setKey(''); setSettings(saved); onSaved(saved)
      if (test) {
        const result = await api<{ text: string; usage: AgentUsage }>('/agent/test', 'POST', {})
        setTested(result.text || '连接正常，工具调用服务已就绪')
      } else close()
    } catch (reason) { setError(errorMessage(reason)) } finally { setBusy(false) }
  }
  return <Surface className="ca-settings" role="region" aria-label="Agent 设置">
    <div className="ca-settings-title"><strong>模型连接</strong><button onClick={close} aria-label="关闭 Agent 设置"><X size={16}/></button></div>
    {settings ? <form onSubmit={event => { event.preventDefault(); void save(false) }}>
      <label>接口协议<GlassSelect label="Agent 接口协议" value={settings.protocol} options={[{ value: 'responses', label: 'Responses' }, { value: 'chat_completions', label: 'Chat Completions' }]} change={value => change({ protocol: value as AgentSettings['protocol'] })}/></label>
      <label>服务地址<DraftInput type="url" aria-label="Agent 服务地址" value={settings.base_url} onValueChange={base_url => change({ base_url })} placeholder="https://api.example.com/v1" required autoComplete="off"/></label>
      <label>模型<DraftInput aria-label="Agent 模型" value={settings.model} onValueChange={model => change({ model })} placeholder="模型名称" required autoComplete="off"/></label>
      <label>API Key<DraftInput type="password" aria-label="Agent API Key" value={key} onValueChange={setKey} placeholder={settings.has_key ? '已保存 · 留空保留原密钥' : '输入 API Key'} autoComplete="new-password" spellCheck={false}/></label>
      <div className="ca-settings-row"><label>超时 / 秒<input type="number" min={15} max={600} value={settings.timeout_seconds} onChange={event => change({ timeout_seconds: Number(event.target.value) })}/></label>
        <label>最大输出 tokens<input type="number" min={1024} max={64000} value={settings.max_output_tokens} onChange={event => change({ max_output_tokens: Number(event.target.value) })}/></label></div>
      <p className="ca-settings-note">密钥保存在本机。会话和工具定义保持稳定前缀；缓存统计以服务返回值为准。</p>
      <div className="ca-settings-actions"><button type="button" disabled={busy} onClick={() => void save(true)}>{busy ? <LoaderCircle size={13} className="ca-spin"/> : <RefreshCw size={13}/>}保存并测试</button><button className="ca-button-primary" disabled={busy} type="submit">保存</button></div>
    </form> : !error && <p className="ca-muted"><LoaderCircle size={14} className="ca-spin"/>读取设置…</p>}
    <Presence>{error && <Surface tone="fade" className="ca-inline-error" role="alert">{error}</Surface>}{tested && <Surface tone="fade" className="ca-test-result" role="status"><Check size={14}/>{tested}</Surface>}</Presence>
  </Surface>
}

export default function AgentPanel(props: Props) {
  const { project, canvas, account, close, sessionId: controlledSessionId } = props
  const latest = useRef(props); latest.current = props
  const [sessions, setSessions] = useState<AgentSessionSummary[]>([]), [session, setSession] = useState<AgentSession | null>(null)
  const [internalSessionId, setInternalSessionId] = useState(''), [liveRun, setLiveRun] = useState<AgentRun | null>(null)
  const [draft, setDraft] = useState(''), [mode, setMode] = useState<AgentMode>('plan'), [error, setError] = useState('')
  const [loading, setLoading] = useState(true), [sending, setSending] = useState(false), [actionBusy, setActionBusy] = useState(false)
  const [showSettings, setShowSettings] = useState(false), [showSessions, setShowSessions] = useState(false), [showMentions, setShowMentions] = useState(false)
  const [mentionQuery, setMentionQuery] = useState(''), [mentionIndex, setMentionIndex] = useState(0)
  const [settings, setSettings] = useState<AgentSettings | null>(null), [excluded, setExcluded] = useState<Set<string>>(new Set())
  const [attached, setAttached] = useState<CreativeNode[]>([]), [width, setWidth] = useState(400)
  const textarea = useRef<HTMLTextAreaElement>(null), scroll = useRef<HTMLDivElement>(null), scrollContent = useRef<HTMLDivElement>(null), follow = useRef(true)
  const sendLock = useRef(false), contextVersion = useRef(0), loadedContext = useRef(''), projectRevision = useRef(project.revision)
  const hydratedSession = useRef('')
  const pendingSend = useRef<{ signature: string; requestId: string } | null>(null)
  const selectedSessionId = controlledSessionId ?? internalSessionId
  const selectedSessionRef = useRef(selectedSessionId); selectedSessionRef.current = selectedSessionId
  const context = project.id + ':' + canvas.id
  const modeKey = (id: string) => `canvas-agent-mode:${context}:${id || 'new'}`
  function savedMode(id: string): AgentMode | undefined {
    try { const value = localStorage.getItem(modeKey(id)); return value === 'plan' || value === 'auto' ? value : undefined } catch { return undefined }
  }
  function rememberMode(value: AgentMode, id = selectedSessionRef.current) {
    setMode(value)
    try { localStorage.setItem(modeKey(id), value) } catch { /* Storage restrictions must not prevent sending in the selected mode. */ }
  }
  const selected = useMemo(() => [...canvas.nodes.filter(node => node.selected), ...attached.flatMap(node => { const current = canvas.nodes.find(item => item.id === node.id); return current ? [current] : [] })].filter((node, index, all) =>
    !excluded.has(node.id) && all.findIndex(item => item.id === node.id) === index), [canvas.nodes, attached, excluded])
  const mentionNodes = useMemo(() => canvas.nodes.filter(node => node.data.kind !== 'group'
    && (!mentionQuery || `${node.data.label} ${node.id}`.toLowerCase().includes(mentionQuery.toLowerCase()))), [canvas.nodes, mentionQuery])
  const mentionMenuId = `ca-mentions-${canvas.id}`
  const latestRun = liveRun ?? session?.runs.at(-1) ?? null
  const running = !!latestRun && agentBusyStatuses.has(latestRun.status)
  const turns = agentTurns(session, liveRun)

  const chooseSession = useCallback((id: string) => {
    selectedSessionRef.current = id
    follow.current = true
    setInternalSessionId(id); latest.current.onSessionChange?.(id); setShowSessions(false)
  }, [])
  const listSessions = useCallback(async () => {
    const current = latest.current, epoch = contextVersion.current
    const result = await api<AgentSessionSummary[]>(`/agent/sessions?project_id=${encodeURIComponent(current.project.id)}&canvas_id=${encodeURIComponent(current.canvas.id)}`)
    if (epoch === contextVersion.current) setSessions(result)
    return result
  }, [])
  useEffect(() => {
    contextVersion.current += 1
    const epoch = contextVersion.current
    loadedContext.current = context; hydratedSession.current = ''; projectRevision.current = project.revision
    setSession(null); setLiveRun(null); setSessions([]); setLoading(true)
    setError(''); setAttached([]); setExcluded(new Set()); setDraft(''); pendingSend.current = null
    void listSessions().then(result => {
      if (epoch !== contextVersion.current) return
      if (!latest.current.sessionId) chooseSession(result[0]?.id ?? '')
    }).catch(reason => { if (epoch === contextVersion.current) setError(errorMessage(reason)) })
      .finally(() => { if (epoch === contextVersion.current) setLoading(false) })
    return () => { contextVersion.current += 1 }
  }, [context, chooseSession, listSessions])
  useEffect(() => {
    let alive = true
    api<AgentSettings>('/agent/settings').then(value => { if (alive) setSettings(value) }).catch(reason => { if (alive) setError(errorMessage(reason)) })
    return () => { alive = false }
  }, [])
  useEffect(() => {
    if (!selectedSessionId || loadedContext.current !== context) { setSession(null); setLiveRun(null); setMode(savedMode('') ?? 'plan'); return }
    if (hydratedSession.current === selectedSessionId) return
    let alive = true
    setLoading(true); setError(''); setSession(null); setLiveRun(null)
    setMode(savedMode(selectedSessionId) ?? 'plan')
    follow.current = true
    api<AgentSession>(`/agent/sessions/${encodeURIComponent(selectedSessionId)}`).then(value => {
      if (!alive) return
      if (value.project_id !== project.id || value.canvas_id !== canvas.id) throw new Error('这个会话属于其它画布')
      hydratedSession.current = value.id
      setMode(savedMode(value.id) ?? value.runs.at(-1)?.mode ?? 'plan')
      setSession(value); setLiveRun(value.runs.find(run => run.id === value.active_run_id) ?? value.runs.at(-1) ?? null)
    }).catch(reason => { if (alive) setError(errorMessage(reason)) }).finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [selectedSessionId, context, project.id, canvas.id])
  useEffect(() => { projectRevision.current = Math.max(projectRevision.current, project.revision) }, [project.revision])

  const runId = liveRun && agentBusyStatuses.has(liveRun.status) ? liveRun.id : null
  useEffect(() => {
    if (!runId) return
    let alive = true, timer: ReturnType<typeof setTimeout> | undefined
    const epoch = contextVersion.current
    async function read() {
      try {
        const run = await api<AgentRun>(`/agent/runs/${encodeURIComponent(runId!)}`)
        if (!alive || epoch !== contextVersion.current) return
        let updatedProject: Project | null = null
        if (run.project_revision != null && run.project_revision > projectRevision.current) {
          updatedProject = await api<Project>(`/projects/${encodeURIComponent(latest.current.project.id)}`)
          if (!alive || epoch !== contextVersion.current) return
        }
        if (!agentBusyStatuses.has(run.status)) {
          const updated = await api<AgentSession>(`/agent/sessions/${encodeURIComponent(run.session_id)}`)
          if (!alive || epoch !== contextVersion.current) return
          await listSessions()
          if (!alive || epoch !== contextVersion.current) return
          if (updatedProject) { projectRevision.current = run.project_revision!; latest.current.onProjectChanged(updatedProject) }
          setSession(updated); setLiveRun(run); latest.current.onSessionsChanged?.()
          setError(value => value.startsWith('进度连接暂时中断') ? '' : value)
          return
        }
        if (updatedProject) { projectRevision.current = run.project_revision!; latest.current.onProjectChanged(updatedProject) }
        setLiveRun(run); setSession(value => value && value.id === run.session_id ? updateSessionRun(value, run) : value)
        setError(value => value.startsWith('进度连接暂时中断') ? '' : value)
        timer = setTimeout(() => void read(), 1600)
      } catch (reason) {
        if (!alive || epoch !== contextVersion.current) return
        setError('进度连接暂时中断，正在查询原任务：' + errorMessage(reason))
        timer = setTimeout(() => void read(), 2500)
      }
    }
    void read()
    return () => { alive = false; if (timer) clearTimeout(timer) }
  }, [runId, selectedSessionId, listSessions])
  useEffect(() => {
    if (follow.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight
  }, [session, liveRun, loading])
  useEffect(() => {
    const content = scrollContent.current
    if (!content) return
    const observer = new ResizeObserver(() => { if (follow.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight })
    observer.observe(content)
    return () => observer.disconnect()
  }, [])

  async function createSession(preserveDraft = false) {
    const epoch = contextVersion.current, selection = selectedSessionRef.current
    if (!preserveDraft) await latest.current.beforeSend?.()
    const current = latest.current
    const result = await api<AgentSession>('/agent/sessions', 'POST', { project_id: current.project.id, canvas_id: current.canvas.id })
    if (epoch !== contextVersion.current || selection !== selectedSessionRef.current) throw new Error('画布或对话已切换，请在当前对话重新发送')
    hydratedSession.current = result.id
    rememberMode(preserveDraft ? mode : 'plan', result.id)
    chooseSession(result.id); setSession(result); setLiveRun(null)
    if (!preserveDraft) { setDraft(''); setAttached([]); setExcluded(new Set()) }
    await listSessions(); latest.current.onSessionsChanged?.()
    return result
  }
  async function newSession() {
    if (sendLock.current) return
    sendLock.current = true; setSending(true); setError('')
    try { await createSession() } catch (reason) { setError(errorMessage(reason)) } finally { sendLock.current = false; setSending(false) }
  }
  async function send() {
    const message = (textarea.current?.value ?? draft).trim()
    if (!message || sendLock.current || running || loading) return
    if (settings && !settings.configured) { setShowSettings(true); setError('请先配置 Agent 模型连接'); return }
    sendLock.current = true; setSending(true); setError(''); follow.current = true
    const epoch = contextVersion.current, selection = selectedSessionRef.current
    try {
      await latest.current.beforeSend?.()
      if (epoch !== contextVersion.current || selection !== selectedSessionRef.current) return
      const current = session ?? await createSession(true)
      if (epoch !== contextVersion.current || current.id !== selectedSessionRef.current) return
      const prior = pendingSend.current
      const selectedNodes = selected.map(node => node.id)
      const signature = JSON.stringify([current.id, message, mode, account, selectedNodes])
      const requestId = prior?.signature === signature ? prior.requestId : uid()
      pendingSend.current = { signature, requestId }
      const run = await api<AgentRun>(`/agent/sessions/${encodeURIComponent(current.id)}/messages`, 'POST', {
        message, mode, account, selected_nodes: selectedNodes, request_id: requestId,
      })
      if (epoch !== contextVersion.current || current.id !== selectedSessionRef.current) return
      pendingSend.current = null
      setSession(updateSessionRun({ ...current, messages: [...current.messages, { role: 'user', content: message }] }, run))
      setLiveRun(run); setDraft(''); setAttached([]); setExcluded(new Set()); setShowMentions(false)
      await listSessions(); latest.current.onSessionsChanged?.()
      requestAnimationFrame(() => textarea.current?.focus())
    } catch (reason) { if (epoch === contextVersion.current) setError(errorMessage(reason)) }
    finally { sendLock.current = false; setSending(false) }
  }
  async function runAction(operation: 'cancel' | 'resume' | 'approve' | 'reject', run = latestRun) {
    if (!run || actionBusy) return
    setActionBusy(true); setError('')
    const epoch = contextVersion.current
    try {
      const path = operation === 'approve' || operation === 'reject' ? 'approval' : operation
      if (path === 'approval' && !run.approval?.call_id) throw new Error('审批信息已变更，请等待任务刷新后重试')
      const result = await api<AgentRun>(`/agent/runs/${encodeURIComponent(run.id)}/${path}`, 'POST', path === 'approval' ? { approved: operation === 'approve', call_id: run.approval!.call_id } : {})
      if (epoch !== contextVersion.current || run.session_id !== selectedSessionRef.current) return
      setLiveRun(result); setSession(value => value ? updateSessionRun(value, result) : value)
      if (!agentBusyStatuses.has(result.status)) {
        const updated = await api<AgentSession>(`/agent/sessions/${encodeURIComponent(result.session_id)}`)
        if (epoch !== contextVersion.current || run.session_id !== selectedSessionRef.current) return
        setSession(updated); await listSessions(); latest.current.onSessionsChanged?.()
      }
    } catch (reason) { setError(errorMessage(reason)) } finally { setActionBusy(false) }
  }
  function mention(node: CreativeNode) {
    const input = textarea.current, value = input?.value ?? draft
    const next = insertAgentMention(value, input?.selectionStart ?? value.length, input?.selectionEnd ?? value.length, node)
    setDraft(next.value); setShowMentions(false); setMentionIndex(0); setMentionQuery('')
    setAttached(previous => previous.some(item => item.id === node.id) ? previous : [...previous, node])
    setExcluded(previous => { const next = new Set(previous); next.delete(node.id); return next })
    requestAnimationFrame(() => { input?.focus(); input?.setSelectionRange(next.caret, next.caret) })
  }
  function removeReference(id: string) {
    setExcluded(value => new Set([...value, id]))
    setDraft(value => value.replaceAll(`@[node:${id}]`, '').trimStart())
  }
  async function attach() {
    try { const node = await latest.current.onAttachAsset?.(); if (node) mention(node) } catch (reason) { setError(errorMessage(reason)) }
  }
  function resizeKey(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault(); setWidth(value => Math.max(320, Math.min(640, value + (event.key === 'ArrowLeft' ? -20 : 20))))
  }
  return <motion.aside initial={{ width: 0, opacity: 0 }} animate={{ width, opacity: 1 }} exit={{ width: 0, opacity: 0 }} transition={{ ...transition, duration: .24 }}
    className="ca-panel" aria-label="创作 Agent" onKeyDown={event => event.stopPropagation()}>
    <div className="ca-panel-inner" style={{ width }}>
      <header className="ca-header"><span className="ca-agent-mark"><Sparkles size={16}/></span>
        <button className="ca-session-trigger" disabled={sending} onClick={() => setShowSessions(value => !value)} aria-expanded={showSessions} title={session?.title ?? '新对话'}><strong>{session?.title ?? '创作 Agent'}</strong><ChevronDown size={13}/></button>
        <button aria-label="新建 Agent 会话" title="新对话" onClick={() => void newSession()} disabled={sending}><Plus size={17}/></button>
        <button aria-label="Agent 设置" title="模型设置" onClick={() => setShowSettings(value => !value)} className={showSettings ? 'active' : ''}><Settings2 size={16}/></button>
        <button aria-label="收起 Agent" title="收起" onClick={close}><ChevronLeft size={18}/></button>
      </header>
      <Presence>{showSessions && <Surface className="ca-session-menu"><div><History size={14}/><strong>此画布的对话</strong><button onClick={() => setShowSessions(false)} aria-label="关闭会话列表"><X size={14}/></button></div>
        {sessions.length ? sessions.map(item => <button key={item.id} disabled={sending} className={item.id === selectedSessionId ? 'active' : ''} onClick={() => chooseSession(item.id)}><MessageSquare size={14}/><span>{item.title || '新对话'}</span>{item.id === selectedSessionId && <Check size={13}/>}</button>) : <p>还没有历史对话</p>}
        <button className="ca-new-conversation" onClick={() => void newSession()} disabled={sending}><Plus size={14}/>新建对话</button>
      </Surface>}</Presence>
      <Presence>{showSettings && <SettingsForm key="settings" close={() => setShowSettings(false)} onSaved={setSettings}/>}</Presence>
      <div className="ca-conversation" ref={scroll} onScroll={() => { const box = scroll.current; if (box) follow.current = box.scrollHeight - box.scrollTop - box.clientHeight < 90 }}><div className="ca-conversation-content" ref={scrollContent}>
        <ProductionLedger key={canvas.id} canvas={canvas}/>
        <Presence>{loading && <Surface className="ca-loading" role="status"><LoaderCircle size={16} className="ca-spin"/>读取对话…</Surface>}</Presence>
        {!loading && !turns.length && <Surface className="ca-welcome"><span className="ca-welcome-mark"><Sparkles size={25}/></span><h2>把想法，变成一段故事</h2><p>一起构思、打磨提示词，或让我在画布上搭好完整工作流。</p>
          <div className="ca-suggestions">{[
            { icon: Clapperboard, title: '从一句话开始', text: '策划一个有转折的 1 分钟电影短片，先给我故事与分镜方案。' },
            { icon: Workflow, title: '帮我连接工作流', text: '根据当前选中的节点，设计首尾帧视频工作流，并在画布上自动连接。' },
            { icon: Image, title: '让角色保持一致', text: '帮我建立固定的角色参考，并设计三个延续同一形象和风格的镜头。' },
          ].map(item => <button key={item.title} onClick={() => { setDraft(item.text); textarea.current?.focus() }}><item.icon size={16}/><span><strong>{item.title}</strong><small>{item.text}</small></span><ChevronRight size={14}/></button>)}</div>
        </Surface>}
        {turns.map((turn, index) => <div className="ca-turn" key={turn.run?.id ?? `turn-${index}`}>
          {turn.user && <UserMessage content={turn.user.content}/>}
          {turn.run && <RunCard run={turn.run} assistantTexts={turn.assistants.map(message => message.content)} action={operation => void runAction(operation, turn.run)} actionBusy={actionBusy}/>}
          {turn.assistants.map((message, messageIndex) => <Surface tone="fade" className="ca-assistant-text" key={messageIndex}><AgentMarkdown text={message.content}/></Surface>)}
        </div>)}
      </div></div>
      <Presence>{error && <Surface className="ca-error" role="alert"><span>{error}</span><button aria-label="关闭 Agent 提示" onClick={() => setError('')}><X size={14}/></button></Surface>}</Presence>
      <div className="ca-composer-wrap">
        <Presence>{selected.length > 0 && <Reveal className="ca-context"><small>当前参考</small><div>{selected.map(node => <Surface key={node.id} className="ca-context-chip" title={node.id}>
          {node.data.asset_id && node.data.media_type?.startsWith('image/') ? <img src={assetUrl(node.data.asset_id)} alt=""/> : <FileText size={12}/>}
          <span>{node.data.label || names[node.data.kind]}</span><button aria-label={`移除参考 ${node.data.label}`} onClick={() => removeReference(node.id)}><X size={11}/></button>
        </Surface>)}</div></Reveal>}</Presence>
        <div className="ca-composer">
          <Presence>{showMentions && <Surface id={mentionMenuId} className="ca-mentions" role="listbox" aria-label="引用画布节点"><div><strong>引用画布节点</strong><button aria-label="关闭引用选择" onClick={() => setShowMentions(false)}><X size={13}/></button></div>
            {mentionNodes.length ? mentionNodes.map((node, index) => <button key={node.id} id={`${mentionMenuId}-${index}`} className={index === mentionIndex ? 'active' : ''} role="option" aria-selected={selected.some(item => item.id === node.id)} onMouseEnter={() => setMentionIndex(index)} onMouseDown={event => event.preventDefault()} onClick={() => mention(node)}>
              {node.data.asset_id && node.data.media_type?.startsWith('image/') ? <img src={assetUrl(node.data.asset_id)} alt=""/> : <FileText size={14}/>}<span>{node.data.label || names[node.data.kind]}</span><small>{names[node.data.kind]}</small>
            </button>) : <p>{canvas.nodes.length ? '未找到匹配的节点' : '在画布中添加节点后，即可引用'}</p>}
          </Surface>}</Presence>
          <DraftTextarea ref={textarea} aria-label="向 Agent 描述创作需求" placeholder={mode === 'plan' ? '聊聊创意、分镜，或想打磨的提示词…' : '描述你想创作的内容，@ 引用画布节点…'}
            aria-controls={showMentions ? mentionMenuId : undefined} aria-activedescendant={showMentions && mentionNodes.length ? `${mentionMenuId}-${mentionIndex}` : undefined}
            value={draft} onValueChange={value => {
              setDraft(value)
              const query = value.slice(0, textarea.current?.selectionStart ?? value.length).match(/@([^\s@]*)$/u)
              setShowMentions(!!query); setMentionQuery(query?.[1] ?? ''); setMentionIndex(0)
            }}
            onKeyDown={event => {
              if (event.nativeEvent.isComposing) return
              if (event.key === 'Escape') { setShowMentions(false); return }
              if (showMentions && mentionNodes.length && ['ArrowDown', 'ArrowUp', 'Enter'].includes(event.key) && !event.shiftKey) {
                event.preventDefault()
                if (event.key === 'Enter') mention(mentionNodes[Math.min(mentionIndex, mentionNodes.length - 1)])
                else setMentionIndex(index => (index + (event.key === 'ArrowDown' ? 1 : -1) + mentionNodes.length) % mentionNodes.length)
                return
              }
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send() }
            }} rows={3} spellCheck={false}/>
          <div className="ca-composer-toolbar"><GlassSelect label="Agent 工作模式" value={mode} options={[{ value: 'auto', label: '自动创作' }, { value: 'plan', label: '策划讨论' }]} change={value => rememberMode(value as AgentMode)}/>
            <button aria-label="引用画布节点" title="引用节点" onClick={() => { setShowMentions(value => !value); setMentionQuery(''); setMentionIndex(0) }} className={showMentions ? 'active' : ''}><AtSign size={16}/></button>
            {props.onAttachAsset && <button aria-label="添加 Agent 参考素材" title="添加参考" onClick={() => void attach()}><Paperclip size={16}/></button>}
            <span/>{running ? <button className="ca-send ca-stop" aria-label="停止 Agent" disabled={actionBusy} onClick={() => void runAction('cancel')}><Square size={12} fill="currentColor"/></button> : <button className="ca-send" aria-label="发送给 Agent" title="发送 · Enter" disabled={!draft.trim() || sending || loading} onClick={() => void send()}>{sending ? <LoaderCircle size={16} className="ca-spin"/> : <ArrowUp size={17}/>}</button>}
          </div>
        </div>
        <div className="ca-footer"><span>{mode === 'plan' ? '策划讨论 · 先完善想法' : '自动创作 · 编排并执行工作流'}</span>{latestRun ? <CacheUsage usage={latestRun.usage}/> : <span>Shift + Enter 换行</span>}</div>
      </div>
    </div>
    <div className="ca-resize" role="separator" aria-label="调整 Agent 侧栏宽度" aria-orientation="vertical" aria-valuenow={width} aria-valuemin={320} aria-valuemax={640} tabIndex={0} onKeyDown={resizeKey}
      onPointerDown={event => { event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId) }}
      onPointerMove={event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) setWidth(value => Math.max(320, Math.min(640, value + event.movementX))) }}
      onPointerUp={event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId) }}/>
  </motion.aside>
}
