import { motion } from 'motion/react'
import { Presence, Surface, transition, useDismiss } from './Motion'
import { useEffect, useRef, useState } from 'react'
import { Film, Image, Music2, Plus, Search, Upload, X } from 'lucide-react'
import { api, assetUrl, upload, type Asset } from './types'

export default function AssetLibrary({close,choose}:{close:()=>void;choose?:(asset:Asset)=>void}){
  useDismiss(true,close)
  const [assets,setAssets]=useState<Asset[]>([]),[query,setQuery]=useState(''),[kind,setKind]=useState('全部'),[error,setError]=useState(''),[busy,setBusy]=useState(false)
  const input=useRef<HTMLInputElement>(null)
  const load=()=>api<Asset[]>('/assets').then(setAssets).catch(e=>setError(e.message))
  useEffect(()=>{load()},[])
  const shown=assets.filter(a=>a.name.toLowerCase().includes(query.toLowerCase())&&(kind==='全部'||a.media_type.startsWith(kind==='图片'?'image/':kind==='视频'?'video/':'audio/')))
  return <Surface tone="fade" className="cc-modal-backdrop" onClick={close}><motion.section initial={{opacity:0,scale:.98}} animate={{opacity:1,scale:1}} exit={{opacity:0,scale:.98}} transition={transition} className="cc-asset-library" role="dialog" aria-modal="true" aria-label="素材库" onClick={e=>e.stopPropagation()}>
    <header><h2>素材库</h2><span>上传与生成的素材都会保存在这里</span><button aria-label="关闭素材库" onClick={close}><X size={20}/></button></header>
    <div className="cc-library-tools"><div>{['全部','图片','视频','音频'].map(k=><button key={k} className={k===kind?'active':''} onClick={()=>setKind(k)}>{k}</button>)}</div><label><Search size={17}/><input aria-label="搜索素材" placeholder="搜索素材" value={query} onChange={e=>setQuery(e.target.value)}/></label><button disabled={busy} className="cc-white-button" onClick={()=>input.current?.click()}><Upload size={16}/>{busy?'上传中…':'上传素材'}</button></div>
    {error&&<p role="alert">{error}</p>}<Presence mode="wait"><Surface tone="fade" key={kind} className="cc-asset-grid">{shown.map(asset=><article key={asset.id}><div>{asset.media_type.startsWith('image/')?<img loading="lazy" src={assetUrl(asset.id)} alt={asset.name}/>:asset.media_type.startsWith('video/')?<video src={assetUrl(asset.id)} controls preload="metadata"/>:<audio src={assetUrl(asset.id)} controls preload="metadata"/>}{choose&&<button aria-label={'添加 '+asset.name+' 到画布'} onClick={()=>choose(asset)}><Plus size={18}/>添加到画布</button>}</div><p title={asset.name}>{asset.media_type.startsWith('image/')?<Image size={14}/>:asset.media_type.startsWith('video/')?<Film size={14}/>:<Music2 size={14}/>}<span>{asset.name}</span></p><small>{asset.width&&asset.height?`${asset.width} × ${asset.height}`:''}{asset.duration?` · ${asset.duration.toFixed(1)}s`:''}</small></article>)}</Surface></Presence>
    {!shown.length&&<p className="cc-library-empty">{assets.length?'没有匹配的素材':'把参考图片上传到这里，开始创作'}</p>}
    <input type="file" ref={input} hidden multiple accept="image/png,image/jpeg,image/webp,video/mp4,video/webm,audio/mpeg,audio/wav" onChange={async e=>{const files=Array.from(e.target.files??[]);e.target.value='';setBusy(true);setError('');try{for(const file of files)await upload(file);await load()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}/>
  </motion.section></Surface>
}
