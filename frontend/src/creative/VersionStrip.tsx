import { Check, Film, Music2 } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { assetUrl, type CardData, type NodeVersion } from './types'
import { versionsOf } from './versions'

export default function VersionStrip({ data, view, select, viewed, deferView = false }: { data: CardData; view: (v: NodeVersion) => void; select: (v: NodeVersion) => void; viewed?: string; deferView?: boolean }) {
  const click = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(click.current), [])
  const versions = versionsOf(data)
  if (!versions.length) return null
  return <div className="cc-version-section nodrag nopan">
    <div><span>版本 · {versions.length}</span><small>单击查看 · 双击设为输出</small></div>
    <div className="cc-versions">{versions.map((v, i) => {
      const active = (data.active_version_id ?? data.asset_id) === v.id
      return <button key={v.id} aria-label={`版本 ${i+1}${active?'，当前输出':''}`} aria-pressed={active}
        className={`${active?'is-output':''} ${viewed===v.id?'is-viewed':''}`} onClick={e => {
          clearTimeout(click.current)
          if (!deferView || e.detail === 0) view(v)
          else if (e.detail === 1) click.current = setTimeout(() => view(v), 400)
        }} onDoubleClick={e => { e.stopPropagation(); clearTimeout(click.current); select(v) }}>
        {v.media_type.startsWith('video') ? <><video src={assetUrl(v.asset_id)} muted preload="metadata"/><Film size={13}/></> : v.media_type.startsWith('audio') ? <Music2 size={20}/> : <img src={assetUrl(v.asset_id)} alt="" draggable={false}/>}
        <span>V{i+1}</span>{active && <i><Check size={12}/></i>}
      </button>
    })}</div>
  </div>
}
