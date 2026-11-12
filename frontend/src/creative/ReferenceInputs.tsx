import { Film, Image, Type, X } from 'lucide-react'
import GlassSelect from './GlassSelect'
import { assetUrl, type CardData } from './types'
import { videoMode, type referencesFor } from './references'

export type References = ReturnType<typeof referencesFor>
export default function ReferenceInputs({data, references, insert, setRole, remove}: {
  data: CardData; references: References; insert: (alias: string) => void;
  setRole: (source: string, role: string) => void; remove: (source: string) => void;
}) {
  const images = references.filter(r=>r.node.data.kind!=='text')
  const mode = videoMode(data, images.length)
  const ordered = [...images].sort((a,b)=>({first:0,base:0,reference:1,last:2}[a.role]??1)-({first:0,base:0,reference:1,last:2}[b.role]??1))
  return !!references.length && <div className="cc-reference-strip nodrag nopan">{references.map(({node,alias,role}) => {
    const text = node.data.kind==='text'
    const effectiveRole = data.kind==='video' && mode!=='参考图' ? (ordered[0]?.node.id===node.id?'first':'last') : data.kind==='image' ? (ordered[0]?.node.id===node.id?'base':'reference') : role
    return <div className="cc-reference-chip" key={node.id}>
      <button title={`插入 @${alias}（${node.data.label}）`} onMouseDown={e=>e.preventDefault()} onClick={()=>insert(alias)}>
        {text?<Type size={17}/>:node.data.media_type?.startsWith('video/')||node.data.kind==='video'?<Film size={17}/>:node.data.asset_id?<img src={assetUrl(node.data.asset_id)} alt=""/>:<Image size={17}/>}
        <span>@{alias} · {node.data.label}</span>
      </button>
      {!text && data.kind==='video' && mode==='首尾帧' && <GlassSelect label={alias+' 的参考用途'} value={effectiveRole} change={v=>setRole(node.id,v)} options={[{value:'first',label:'首帧'},{value:'last',label:'尾帧'}]}/>}
      {!text && data.kind==='image' && images.length>1 && <GlassSelect label={alias+' 的参考用途'} value={effectiveRole} change={v=>setRole(node.id,v)} options={[{value:'base',label:'主图'},{value:'reference',label:'参考'}]}/>}
      {!text && data.kind==='video' && mode!=='首尾帧' && <small>{mode==='参考图'?'主体参考':mode==='首尾循环'?'同图首尾':'首帧'}</small>}
      <button aria-label={'移除引用 '+alias} title="断开引用" onClick={()=>remove(node.id)}><X size={12}/></button>
    </div>
  })}</div>
}
