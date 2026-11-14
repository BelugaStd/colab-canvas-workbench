import { useId, useState } from 'react'
import { Box, Camera, ChevronDown, ClipboardList, LockKeyhole, MapPin, UserRound } from 'lucide-react'
import { Presence, Reveal } from './Motion'
import { assetUrl, type CanvasData, type Production, type ProductionShot } from './types'
import './production.css'

const entityKinds = { character: { name: '人物', icon: UserRound }, location: { name: '场景', icon: MapPin }, prop: { name: '道具', icon: Box } }
const displayValue = (value: unknown): string => typeof value === 'string' ? value : JSON.stringify(value) ?? '—'
const durationText = (seconds: number) => seconds >= 60 ? `${Math.floor(seconds / 60)}分${seconds % 60 ? `${Number((seconds % 60).toFixed(2))}秒` : ''}` : `${seconds}秒`

function StateView({ label, value }: { label: string; value: Record<string, unknown> }) {
  return <div className="cp-state"><h5>{label}</h5><dl>{Object.entries(value).map(([key, item]) => <div key={key}><dt>{key}</dt><dd>{displayValue(item)}</dd></div>)}</dl></div>
}

function Shot({ shot, order, production, canvas }: { shot: ProductionShot; order: number; production: Production; canvas: CanvasData }) {
  const [open, setOpen] = useState(false), bodyId = useId()
  const scene = production.scenes.find(item => item.id === shot.scene_id)
  const image = canvas.nodes.find(node => node.id === shot.image_node_id)?.data
  const endImage = canvas.nodes.find(node => node.id === shot.end_image_node_id)?.data
  const camera = shot.camera
  return <article className="cp-shot">
    <button className="cp-shot-heading" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(value => !value)}>
      <span className="cp-shot-number">{String(order).padStart(2, '0')}</span><span><strong>{shot.purpose}</strong><small>{scene?.name ?? shot.scene_id} · {durationText(shot.duration)}</small></span>
      <ChevronDown size={13} style={{ rotate: open ? '180deg' : '0deg' }}/>
    </button>
    <Presence>{open && <Reveal key="details"><div className="cp-shot-details" id={bodyId}>
      {(image?.asset_id || endImage?.asset_id) && <div className="cp-endpoints">{[{ label: '首帧', data: image }, { label: '尾帧', data: endImage }].map(({ label, data }) => data?.asset_id && <figure key={label}><img src={assetUrl(data.asset_id)} alt={`${shot.id} ${label}`} loading="lazy"/><figcaption>{label}</figcaption></figure>)}</div>}
      <p className="cp-action">{shot.action}</p>
      {((shot.start ?? 0) > 0 || (shot.source_duration ?? Math.max(5, shot.duration)) !== shot.duration) && <p className="cp-trim">原片 {durationText(shot.source_duration ?? Math.max(5, shot.duration))} · 从 {durationText(shot.start ?? 0)} 截取 {durationText(shot.duration)}</p>}
      {shot.transition && <p className="cp-trim">镜头过渡 · {shot.transition}</p>}
      <div className="cp-shot-id"><Camera size={11}/><code>{shot.id}</code><span>节点</span><code>{shot.node_id}</code></div>
      <dl className="cp-camera">{[
        ['景别 / 焦段', `${camera.size} · ${camera.lens_mm}mm`], ['运镜', camera.movement], ['运镜动机', camera.motivation], ['轴线', camera.axis], ['视线', camera.eyeline],
      ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
      <div className="cp-state-pair"><StateView label="入场状态" value={shot.state_in}/><StateView label="出场状态" value={shot.state_out}/></div>
      <div className="cp-shot-bindings"><h5>绑定版本</h5>{shot.bindings.length ? shot.bindings.map(id => {
        const lock = production.locks.find(item => item.id === id)
        return <span className="cp-bound-version" key={id}><LockKeyhole size={10}/><span>{lock?.label ?? '版本缺失'}</span><code>{id}</code></span>
      }) : <p className="cp-empty">尚未绑定锁定版本</p>}</div>
      {!!shot.dialogue?.length && <div className="cp-dialogue"><h5>对白</h5>{shot.dialogue.map((line, index) => <p key={index}><strong>{production.entities.find(entity => entity.id === line.speaker)?.name ?? line.speaker}</strong><span>{line.text}</span></p>)}</div>}
    </div></Reveal>}</Presence>
  </article>
}

export default function ProductionLedger({ canvas }: { canvas: CanvasData }) {
  const [open, setOpen] = useState(false), [tab, setTab] = useState<'shots' | 'assets' | 'scenes'>('shots'), bodyId = useId()
  const record = canvas.production
  const production = record && { ...record, entities: record.entities ?? [], scenes: record.scenes ?? [], shots: record.shots ?? [], locks: record.locks ?? [] }
  if (!production) return null
  const total = production.shots.reduce((sum, shot) => sum + shot.duration, 0)
  return <section className="cp-ledger" aria-label="制作台账">
    <button className="cp-ledger-heading" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(value => !value)}>
      <ClipboardList size={14}/><strong>制作台账</strong><span>{production.shots.length} 镜 · {durationText(total || production.brief.target_seconds)}</span><ChevronDown size={13} style={{ rotate: open ? '180deg' : '0deg' }}/>
    </button>
    <Presence>{open && <Reveal key="ledger"><div className="cp-ledger-body" id={bodyId}>
      <div className="cp-brief"><div><strong>{production.brief.title}</strong><small>计划 v{production.revision} · 目标 {durationText(production.brief.target_seconds)}</small></div><p>{production.brief.conflict}</p><p>{production.brief.arc}</p></div>
      <div className="cp-tabs" role="tablist" aria-label="制作台账分类">{([
        ['shots', '镜头', production.shots.length], ['assets', '资产', production.entities.length], ['scenes', '场次', production.scenes.length],
      ] as const).map(([value, label, count]) => <button key={value} role="tab" id={`${bodyId}-${value}`} aria-controls={`${bodyId}-content`} aria-selected={tab === value} tabIndex={tab === value ? 0 : -1} onClick={() => setTab(value)} onKeyDown={event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
        event.preventDefault()
        const tabs = ['shots', 'assets', 'scenes'] as const, index = tabs.indexOf(tab)
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (index + (event.key === 'ArrowRight' ? 1 : -1) + 3) % 3
        setTab(tabs[next]); event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus()
      }}>{label}<small>{count}</small></button>)}</div>
      <div className="cp-tab-content" role="tabpanel" id={`${bodyId}-content`} aria-labelledby={`${bodyId}-${tab}`}>
        <Presence mode="wait"><Reveal key={tab}>
          {tab === 'shots' && <div className="cp-shots">{production.shots.length ? production.shots.map((shot, index) => <Shot key={shot.id} shot={shot} order={index + 1} production={production} canvas={canvas}/>) : <p className="cp-empty">镜头尚在策划中</p>}</div>}
          {tab === 'assets' && <div className="cp-entities">{production.entities.length ? production.entities.map(entity => {
            const kind = entityKinds[entity.kind], locks = production.locks.filter(lock => lock.entity_id === entity.id)
            return <article className="cp-entity" key={entity.id}><div className="cp-entity-heading"><kind.icon size={12}/><strong>{entity.name}</strong><small>{kind.name}</small></div><p>{entity.description}</p>
              {locks.length ? locks.map(lock => <div className="cp-locked-asset" key={lock.id}><img src={assetUrl(lock.asset_id)} alt={`${entity.name} ${lock.id}`} loading="lazy"/><div><strong><LockKeyhole size={10}/>{lock.label}</strong><code>{lock.id}</code><small title={lock.asset_id}>素材 {lock.asset_id}</small></div></div>) : <span className="cp-empty">尚未锁定版本</span>}
            </article>
          }) : <p className="cp-empty">人物、场景与道具尚在策划中</p>}</div>}
          {tab === 'scenes' && <div className="cp-scenes">{production.scenes.length ? production.scenes.map((scene, index) => <article className="cp-scene" key={scene.id}><h4><span>{String(index + 1).padStart(2, '0')}</span>{scene.name}</h4><p><MapPin size={11}/>{production.entities.find(entity => entity.id === scene.location_id)?.name ?? scene.location_id}</p><dl><div><dt>轴线</dt><dd>{scene.axis}</dd></div><div><dt>光线</dt><dd>{scene.lighting}</dd></div></dl></article>) : <p className="cp-empty">场次尚在策划中</p>}</div>}
        </Reveal></Presence>
      </div>
      <p className="cp-ledger-note">通过对话调整策划、版本绑定与镜头安排</p>
    </div></Reveal>}</Presence>
  </section>
}
