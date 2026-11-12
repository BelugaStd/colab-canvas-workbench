import GlassSelect from './GlassSelect'
import { createContext, useContext, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Handle, NodeResizer, Position, useViewport, type NodeProps } from '@xyflow/react'
import { ArrowUp, Download, Film, Image, Maximize2, MoreHorizontal, Music2, Pencil, Plus, SlidersHorizontal, Type, Upload, X } from 'lucide-react'
import { assetUrl, names, type CardData, type CreativeNode } from './types'
import { motion } from 'motion/react'
import { Presence, Reveal, Surface, transition, useDismiss } from './Motion'
import CreationSettings from './CreationSettings'
import Media from './Media'
import VersionStrip from './VersionStrip'
import { selectVersion, versionsOf } from './versions'
import { activeStatuses, statusLabel, type Job } from '../types'

export const CardContext = createContext<{
  update: (id: string, data: Partial<CardData>) => void; references: (id: string) => CreativeNode[];
  generate: (id: string) => void; upload: (id: string) => void; preview: (id: string, versionId?:string) => void;
  job: (id:string)=>Job|undefined; jobAction: (id:string,operation:'cancel'|'resume')=>void;
  referenceRole: (id:string,source:string)=>string; setReferenceRole: (id:string,source:string,role:string)=>void;
  previewOpen: boolean;
}>({ update: () => {}, references: () => [], generate: () => {}, upload: () => {}, preview: () => {}, job:()=>undefined,jobAction:()=>{},referenceRole:()=>'',setReferenceRole:()=>{},previewOpen:false })
export const kindIcons = { image: Image, video: Film, text: Type, upload: Upload, upscale: Maximize2, audio: Music2, group: MoreHorizontal }

export default function Card({ id, data, selected, positionAbsoluteX, positionAbsoluteY }: NodeProps<CreativeNode>) {
  const ctx = useContext(CardContext), Icon = kindIcons[data.kind]
  const [renaming, setRenaming] = useState(false)
  const [settings, setSettings] = useState(false), [mention, setMention] = useState<string | null>(null)
  useDismiss(settings,()=>setSettings(false))
  const root = useRef<HTMLDivElement>(null), viewport = useViewport()
  const [overlay, setOverlay] = useState<{ parent: Element; left: number; top: number } | null>(null)
  const promptRef = useRef<HTMLTextAreaElement>(null)
  const references = ctx.references(id), video = data.kind === 'video', text = data.kind === 'text'
  const editable = ['image', 'video', 'upscale', 'text'].includes(data.kind)
  const hasMedia = !!data.asset_id, url = data.asset_id ? assetUrl(data.asset_id) : undefined
  const outputSettings = versionsOf(data).find(v=>v.asset_id===data.asset_id)?.settings ?? data
  const update = (values: Partial<CardData>) => ctx.update(id, values)
  const job=ctx.job(id), running=!!job&&activeStatuses.has(job.status)
  useLayoutEffect(() => {
    if (!settings || !selected || !root.current) return
    const parent = root.current.closest('.cc-canvas')
    if (!parent) return
    const bounds = parent.getBoundingClientRect(), node = root.current.getBoundingClientRect()
    const height = video ? 510 : data.kind === 'upscale' ? 150 : 340
    setOverlay({ parent, left: Math.max(12, Math.min(node.left - bounds.left + 36, bounds.width - 392)),
      top: Math.max(12, Math.min(node.bottom - bounds.top - height - 20, bounds.height - height - 20)) })
  }, [settings, selected, viewport.x, viewport.y, viewport.zoom, video, data.kind, positionAbsoluteX, positionAbsoluteY])
  function setPrompt(value: string) { update({ prompt: value }); setMention(value.match(/@([^@\s]*)$/)?.[1] ?? null) }
  function insertMention(node: CreativeNode) { update({ prompt: data.prompt.replace(/@[^@\s]*$/, '@' + node.data.label + ' ') }); setMention(null); promptRef.current?.focus() }
  if(data.kind==='group')return <div className="cc-group-card"><NodeResizer isVisible={selected} minWidth={300} minHeight={200}/><input className="nodrag" aria-label="分组名称" value={data.label} onChange={e=>update({label:e.target.value})}/></div>
  return <motion.div layout="size" initial={{opacity:0}} animate={{opacity:1}} transition={{...transition,duration:.22}} ref={root} className={`cc-card cc-${data.kind} ${selected ? 'is-selected' : ''} ${hasMedia ? 'has-media' : ''}`} data-testid={`card-${data.kind}`}>
    <div className="cc-card-title"><Icon size={16} />{renaming ? <input autoFocus onBlur={()=>setRenaming(false)} onKeyDown={e=>{if(e.key==='Enter'||e.key==='Escape')setRenaming(false)}} aria-label="节点名称" className="nodrag" value={data.label} maxLength={100} onChange={e => update({ label: e.target.value })} /> : <span onDoubleClick={()=>setRenaming(true)}>{data.label}</span>}</div>
    <Presence>{selected && <Surface className="cc-node-toolbar nodrag nopan"><button title="重命名节点" onClick={()=>setRenaming(true)}><Pencil size={18}/></button><i /><button title="上传素材" onClick={() => ctx.upload(id)}><Upload size={19} /></button>{hasMedia && <><i /><button title="预览素材" onClick={() => ctx.preview(id)}><Maximize2 size={19} /></button></>}</Surface>}</Presence>
    <Handle type="target" id="input" position={Position.Left} className="cc-port"><Plus size={13} /></Handle>
    <Handle type="source" id="output" position={Position.Right} className="cc-port"><Plus size={13} /></Handle>
    <div className="cc-card-body">
      {job&&<div className={`cc-job-status nodrag nopan ${job.status}`} role="status"><span>{statusLabel[job.status]}{running&&job.progress!=null?` · ${job.progress}%`:''}</span><small title={job.message}>{job.message}</small>{running&&job.status!=='downloading'&&<button onClick={()=>ctx.jobAction(job.id,job.status==='recovering'?'resume':'cancel')}>{job.status==='recovering'?'恢复查询':'取消'}</button>}</div>}
      <Presence mode="wait">{hasMedia && <Surface key={data.asset_id} tone="fade"><Media id={data.asset_id!} type={data.media_type} label={data.label} expand={()=>ctx.preview(id)} suspended={ctx.previewOpen}/></Surface>}</Presence>
      <Presence>{!selected && !hasMedia && <Surface tone="fade" className="cc-empty-node"><Icon size={30} strokeWidth={1.2} /><span>{data.prompt || (data.kind === 'upload' ? '拖入文件或点击上传' : '自己写提示词')}</span><small>{data.kind === 'video' ? 'MiniMax H3' : data.kind === 'image' ? (data.model === 'moss-image' ? 'MOSS Image' : 'MiniMax Image') : names[data.kind]}</small></Surface>}</Presence>
      {selected && data.kind === 'upload' && !hasMedia && <button className="cc-upload-target nodrag" onClick={() => ctx.upload(id)}><Upload size={32} /><span>上传图片、视频或音频</span><small>也可以将文件拖到画布上</small></button>}
      <Presence>{selected && editable && <Reveal className="cc-editor-reveal"><div className="cc-editor">
        {data.kind === 'upscale' ? <div className="cc-upscale-info"><Maximize2 size={30} /><strong>让画面更清晰</strong><small>连接图片作为输入，再选择放大倍数</small></div> : <textarea className="nodrag nopan" ref={promptRef} aria-label={video ? '描述这段视频' : text ? '文本内容' : '描述这张图片'} value={data.prompt} placeholder={video ? '描述这段视频，输入 @ 引用已连接的素材…' : text ? '写下你的创意…' : '描述你想要的画面，输入 @ 添加参考…'} onChange={e => setPrompt(e.target.value)} onKeyDown={e => { if (e.key === 'Escape') setMention(null) }} spellCheck={false} />}
        <Presence>{mention !== null && <Surface className="cc-mentions"><div><Image size={15} />引用已连接的素材<button title="关闭引用" onClick={() => setMention(null)}><X size={14} /></button></div>{references.filter(n => n.data.label.toLowerCase().includes(mention.toLowerCase())).map(n => <button key={n.id} onClick={() => insertMention(n)}>{n.data.asset_id ? <img src={assetUrl(n.data.asset_id)} alt="" /> : <Image size={18} />}<span>{n.data.label}</span><small>{n.data.asset_id ? '素材' : '等待生成'}</small></button>)}{!references.length && <p>先将素材的输出端连接到此节点</p>}</Surface>}</Presence>
        {!!references.length && <div className="cc-reference-strip nodrag nopan">{references.map(n => <div className="cc-reference-chip" key={n.id}><button title={'引用 ' + n.data.label} onClick={() => { update({ prompt: data.prompt + '@' + n.data.label + ' ' }); promptRef.current?.focus() }}>{n.data.asset_id ? <img src={assetUrl(n.data.asset_id)} alt="" /> : <Image size={17} />}<span>{n.data.label}</span></button>{video&&n.data.kind!=='text'&&<GlassSelect label={n.data.label+' 的参考用途'} value={ctx.referenceRole(id,n.id)} change={v=>ctx.setReferenceRole(id,n.id,v)} options={[{value:"reference",label:"自动"},{value:"first",label:"首帧"},{value:"last",label:"尾帧"}]}/>}</div>)}</div>}
        {!text && <div className="cc-editor-bottom nodrag nopan"><button aria-label="添加参考文件" title="添加参考文件" onClick={() => ctx.upload(id)}><Plus size={17} /></button><button aria-label="节点设置" title="节点设置" className={settings ? 'active' : ''} onClick={() => setSettings(!settings)}><SlidersHorizontal size={17} /></button><span className="cc-model-label">{video ? 'MiniMax H3' : data.kind === 'upscale' ? 'Real-ESRGAN' : data.model === 'moss-image' ? 'MOSS Image' : 'MiniMax Image'}</span><button className="cc-generate" aria-label={hasMedia?'重新生成':'生成'} title={hasMedia?'重新生成（保留历史版本）':'生成'} disabled={running || (data.kind !== 'upscale' && !data.prompt.trim())} onClick={() => ctx.generate(id)}><ArrowUp size={21} /></button></div>}
      </div></Reveal>}</Presence>
      {overlay && createPortal(<Presence>{selected && settings && !text && <Surface className="cc-settings nodrag nopan" style={{left:overlay.left,top:overlay.top}} onPointerDown={e => e.stopPropagation()}>
        <header><span>创作设置</span><button aria-label="关闭设置" onClick={() => setSettings(false)}><X size={17} /></button></header>
        <CreationSettings data={data} update={update}/>
      </Surface>}</Presence>, overlay.parent)}
      <Presence>{selected && hasMedia && <Reveal key="versions"><VersionStrip deferView data={data} view={v=>ctx.preview(id,v.id)} select={v=>ctx.update(id,selectVersion(data,v))}/></Reveal>}</Presence>
    </div>
    {hasMedia && !selected && <div className="cc-card-caption">{data.media_type?.startsWith('video') ? <Film size={13} /> : <Image size={13} />}<span>{data.kind === 'video' ? `MiniMax H3 · ${outputSettings.duration??data.duration}s` : outputSettings.model === 'moss-image' ? 'MOSS Image' : data.label}</span><a title="下载" href={url} download><Download size={15} /></a></div>}
  </motion.div>
}
