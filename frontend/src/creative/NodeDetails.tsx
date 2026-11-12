import { useEffect, useRef, useState } from 'react'
import { ArrowUp, Check, Download, RotateCcw, X } from 'lucide-react'
import { activeStatuses, type Job } from '../types'
import { assetUrl, type CardData, type CreativeNode } from './types'
import { selectVersion, versionsOf } from './versions'
import { Presence, Surface } from './Motion'
import VersionStrip from './VersionStrip'
import CreationSettings from './CreationSettings'
import { DraftTextarea } from './DraftText'
import ReferenceInputs, { type References } from './ReferenceInputs'
import { insertReference, referenceIssue } from './references'

export default function NodeDetails({ node, initialVersion, update, generate, close, job, videoUpscale=false, references, setRole, removeReference }: {
  node: CreativeNode; initialVersion?: string; update: (data: Partial<CardData>) => void;
  generate: () => void; close: () => void; job?: Job; videoUpscale?: boolean;
  references: References; setRole: (source:string,role:string)=>void; removeReference:(source:string)=>void;
}) {
  const [viewed, setViewed] = useState(initialVersion ?? node.data.active_version_id ?? node.data.asset_id)
  const activeId = node.data.active_version_id ?? node.data.asset_id, previousActive = useRef(activeId)
  useEffect(() => {
    if (previousActive.current !== activeId) { setViewed(activeId); previousActive.current = activeId }
  }, [activeId])
  const versions = versionsOf(node.data), version = versions.find(v => v.id === viewed) ?? versions.at(-1)
  const running = !!job && activeStatuses.has(job.status)
  const promptRef=useRef<HTMLTextAreaElement>(null), issue=referenceIssue(node.data,references)
  function insert(alias:string) {
    const input=promptRef.current,value=input?.value??node.data.prompt
    const next=insertReference(value,input?.selectionStart??value.length,input?.selectionEnd??value.length,alias)
    update({prompt:next.value});requestAnimationFrame(()=>{input?.focus();input?.setSelectionRange(next.caret,next.caret)})
  }
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (!e.isComposing && e.key === 'Escape') { e.stopPropagation(); close() } }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [close])
  if (!version) return null
  const active = (node.data.active_version_id ?? node.data.asset_id) === version.id
  return <Surface tone="fade" className="cc-modal-backdrop" onClick={close}>
    <Surface role="dialog" aria-modal="true" aria-label="节点详情" className="cc-node-details" onClick={e => e.stopPropagation()}>
      <header><div><strong>{node.data.label}</strong><span>V{versions.indexOf(version)+1}{active?' · 当前输出':''}</span></div>
        <a href={assetUrl(version.asset_id)} download title="下载此版本"><Download size={18}/></a>
        <button autoFocus aria-label="关闭节点详情" onClick={close}><X size={20}/></button></header>
      <div className="cc-detail-body"><div className="cc-detail-left">
        <div className="cc-detail-media"><Presence mode="wait"><Surface key={version.id} tone="fade">
          {version.media_type.startsWith('video') ? <video controls autoPlay loop playsInline src={assetUrl(version.asset_id)}/> : version.media_type.startsWith('audio') ? <audio controls src={assetUrl(version.asset_id)}/> : <img src={assetUrl(version.asset_id)} alt={node.data.label} draggable={false}/>}
        </Surface></Presence></div>
        <div className="cc-version-description"><p>{version.settings?.prompt || (version.settings?'未填写提示词':'此历史版本未记录生成参数')}</p><small>{version.note ?? (version.created_at ? new Date(version.created_at*1000).toLocaleString() : '原有素材')}
          {version.settings && ` · ${[version.settings.model,version.settings.resolution,version.settings.aspect,node.data.kind==='video'&&version.settings.duration?version.settings.duration+'s':'',node.data.kind==='video'&&version.settings.fps?version.settings.fps+'fps':''].filter(Boolean).join(' · ')}`}</small></div>
        {!!version.settings?.reference_inputs?.length&&<div className="cc-reference-strip"><small>生成时输入</small>{version.settings.reference_inputs.map(ref=><span className="cc-reference-chip" key={ref.node_id} title={ref.text??ref.label}>{ref.asset_id&&<img width="28" height="28" src={assetUrl(ref.asset_id)} alt={ref.label}/>}@{ref.alias} · {ref.label} · {({base:'主图',first:'首帧',last:'尾帧',reference:'参考'} as Record<string,string>)[ref.role]}</span>)}</div>}
        <VersionStrip data={node.data} viewed={version.id} view={v => setViewed(v.id)} select={v => { update(selectVersion(node.data,v)); setViewed(v.id) }}/>
        <button className={`cc-output-choice ${active?'active':''}`} disabled={active} onClick={() => update(selectVersion(node.data,version))}><Check size={15}/>{active?'当前输出 · 下游使用此版本':'设为当前输出'}</button>
      </div>
      {['image','video','upscale','text'].includes(node.data.kind) && <aside className="cc-detail-editor">
        <h3>调整并重新生成</h3><p>新结果保存在这个节点，已有版本会保留。</p>
        {node.data.kind !== 'upscale' && <DraftTextarea ref={promptRef} aria-label="详情提示词" value={node.data.prompt} onValueChange={prompt => update({prompt})} placeholder="描述你想要的画面…"/>}
        <ReferenceInputs data={node.data} references={references} insert={insert} setRole={setRole} remove={removeReference}/>
        <CreationSettings data={node.data} update={update} videoUpscale={videoUpscale} referenceCount={references.filter(r=>r.node.data.kind!=='text').length}/>
        {version.settings && <button className="cc-restore-settings" title="恢复提示词和参数，保留当前连线" onClick={() => update({...version.settings})}><RotateCcw size={14}/>使用此版本参数 · 保留当前连线</button>}
        {issue&&<p className="cc-setting-hint" role="status">{issue}</p>}
        <button className="cc-white-button" disabled={running || !!issue} onClick={generate}><ArrowUp size={16}/>{running?'正在生成…':'重新生成'}</button>
      </aside>}</div>
    </Surface>
  </Surface>
}
