import { useEffect, useRef, useState } from 'react'
import { ArrowUp, Check, Download, RotateCcw, X } from 'lucide-react'
import { activeStatuses, type Job } from '../types'
import { assetUrl, type CardData, type CreativeNode } from './types'
import { selectVersion, versionsOf } from './versions'
import { Presence, Surface } from './Motion'
import VersionStrip from './VersionStrip'
import CreationSettings from './CreationSettings'

export default function NodeDetails({ node, initialVersion, update, generate, close, job }: {
  node: CreativeNode; initialVersion?: string; update: (data: Partial<CardData>) => void;
  generate: () => void; close: () => void; job?: Job;
}) {
  const [viewed, setViewed] = useState(initialVersion ?? node.data.active_version_id ?? node.data.asset_id)
  const activeId = node.data.active_version_id ?? node.data.asset_id, previousActive = useRef(activeId)
  useEffect(() => {
    if (previousActive.current !== activeId) { setViewed(activeId); previousActive.current = activeId }
  }, [activeId])
  const versions = versionsOf(node.data), version = versions.find(v => v.id === viewed) ?? versions.at(-1)
  const running = !!job && activeStatuses.has(job.status)
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); close() } }
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
        <VersionStrip data={node.data} viewed={version.id} view={v => setViewed(v.id)} select={v => { update(selectVersion(node.data,v)); setViewed(v.id) }}/>
        <button className={`cc-output-choice ${active?'active':''}`} disabled={active} onClick={() => update(selectVersion(node.data,version))}><Check size={15}/>{active?'当前输出 · 下游使用此版本':'设为当前输出'}</button>
      </div>
      {['image','video','upscale','text'].includes(node.data.kind) && <aside className="cc-detail-editor">
        <h3>调整并重新生成</h3><p>新结果保存在这个节点，已有版本会保留。</p>
        {node.data.kind !== 'upscale' && <textarea aria-label="详情提示词" value={node.data.prompt} onChange={e => update({prompt:e.target.value})} placeholder="描述你想要的画面…"/>}
        <CreationSettings data={node.data} update={update}/>
        {version.settings && <button className="cc-restore-settings" onClick={() => update({...version.settings})}><RotateCcw size={14}/>使用此版本参数</button>}
        <button className="cc-white-button" disabled={running || (node.data.kind !== 'upscale' && !node.data.prompt.trim())} onClick={generate}><ArrowUp size={16}/>{running?'正在生成…':'重新生成'}</button>
      </aside>}</div>
    </Surface>
  </Surface>
}
