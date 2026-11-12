import { motion } from 'motion/react'
import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react'
import { ReactFlow, ReactFlowProvider, Background, MiniMap, SelectionMode, applyNodeChanges, applyEdgeChanges, useReactFlow, type Connection, type Edge, type XYPosition } from '@xyflow/react'
import { Copy, Hand, Image, ListFilter, Map, Maximize2, MousePointer2, Plus, Redo2, Search, Trash2, Undo2, Upload, X, ZoomIn, ZoomOut } from 'lucide-react'
import Card, { CardContext, kindIcons } from './Card'
import { assetUrl, names, newNode, uid, upload, type Asset, type CanvasData, type CardData, type CreativeNode, type Kind } from './types'
import NodeDetails from './NodeDetails'
import { outputKind, upscaleInputIsVideo } from './upscale'
import { Presence, Surface, transition } from './Motion'
import '@xyflow/react/dist/style.css'
import type { Job } from '../types'
import { attachReference, normalizeReferences, referencesFor, setReferenceRole, videoMode } from './references'

const nodeTypes = { creative: Card }
const nodeKinds: Kind[] = ['text', 'image', 'video', 'upscale', 'upload']
const descriptions: Partial<Record<Kind, string>> = { text: '脚本、广告词、品牌文案', image: '宣传图、海报、封面', video: '宣传视频、动画、电影', upscale: '图片、视频清晰度增强', upload: '图片、视频、音频，放到画布上' }
const shortcuts: Partial<Record<Kind, string>> = { text: 'T', image: 'I', video: 'V', upload: 'U' }

function CanvasInner({ canvas, change, fail, generate, addRequest = 0, jobs = [], jobAction, assetRequest }: { canvas: CanvasData; change: (c: CanvasData) => void; fail: (message: string) => void; generate: (id: string|string[]) => void; addRequest?: number; jobs?: Job[]; jobAction: (id:string, operation:'cancel'|'resume')=>void; assetRequest?: (Asset&{requestId:string})|null }) {
  const flow = useReactFlow<CreativeNode>(), wrapper = useRef<HTMLDivElement>(null), file = useRef<HTMLInputElement>(null)
  const [adding, setAdding] = useState<{ x: number; y: number; point?: XYPosition } | null>(null)
  const [menu, setMenu] = useState<{ x: number; y: number; node?: string; point: XYPosition } | null>(null)
  const [minimap, setMinimap] = useState(false), [search, setSearch] = useState<string | null>(null), [hand, setHand] = useState(true)
  const [preview, setPreview] = useState<{id:string;versionId?:string} | null>(null), [uploading, setUploading] = useState(false), [zoom, setZoom] = useState(canvas.viewport.zoom)
  const history = useRef<{ past: CanvasData[]; future: CanvasData[] }>({ past: [], future: [] })
  const clipboard = useRef<{ nodes: CreativeNode[]; edges: Edge[] } | null>(null), uploadTarget = useRef<string | null>(null)
  const previousAddRequest = useRef(addRequest)
  useEffect(() => {
    if (previousAddRequest.current === addRequest) return
    previousAddRequest.current = addRequest
    setAdding({ x: Math.max(12, (wrapper.current?.clientWidth ?? 900) / 2 - 117), y: (wrapper.current?.clientHeight ?? 800) - 330 })
  }, [addRequest])
  const current = useRef(canvas); current.current = canvas
  const appliedAsset=useRef(assetRequest?.requestId)
  const commit = useCallback((next: CanvasData, remember = true) => {
    if (remember) { history.current.past.push(structuredClone(current.current)); history.current.past = history.current.past.slice(-100); history.current.future = [] }
    current.current = next; change(next)
  }, [change])
  useEffect(() => { const next = normalizeReferences(current.current); if (next !== current.current) commit(next, false) }, [canvas.id, commit])
  useEffect(()=>{
    if(!assetRequest||appliedAsset.current===assetRequest.requestId)return
    appliedAsset.current=assetRequest.requestId
    const rect=wrapper.current?.getBoundingClientRect()
    if(!rect)return
    const node=newNode('upload',flow.screenToFlowPosition({x:rect.left+rect.width/2,y:rect.top+rect.height/2}))
    Object.assign(node.data,{asset_id:assetRequest.id,media_type:assetRequest.media_type,label:assetRequest.name.slice(0,100)})
    commit({...current.current,nodes:[...current.current.nodes,node]})
  },[assetRequest,commit,flow])
  function undo(redo = false) {
    const from = redo ? history.current.future : history.current.past, to = redo ? history.current.past : history.current.future
    const next = from.pop(); if (!next) return
    to.push(structuredClone(current.current)); commit(next, false)
  }
  function center() {
    const rect = wrapper.current!.getBoundingClientRect()
    return flow.screenToFlowPosition({ x: rect.left + rect.width / 2 - 100, y: rect.top + rect.height / 2 - 100 })
  }
  function add(kind: Kind, point = adding?.point ?? center()) {
    setAdding(null); setMenu(null)
    if (kind === 'upload') { uploadTarget.current = null; file.current?.click(); return }
    const node = newNode(kind, point)
    const count=current.current.nodes.filter(n=>n.data.kind===kind).length
    node.data.label=names[kind]+(count?' '+(count+1):'')
    if (kind === 'upscale') node.data.resolution = '2×'
    node.selected = true
    commit({ ...current.current, nodes: [...current.current.nodes.map(n => ({ ...n, selected: false })), node] })
  }
  function update(id: string, values: Partial<CardData>) {
    const refs = referencesFor(id, current.current).filter(r=>r.node.data.kind!=='text')
    const node = current.current.nodes.find(n=>n.id===id)
    const modeChanged = values.mode && node && values.mode !== videoMode(node.data, refs.length)
    commit({ ...current.current, nodes: current.current.nodes.map(n => n.id === id ? { ...n, data: { ...n.data, ...values } } : n),
      edges: modeChanged ? current.current.edges.map(e=>e.target===id&&refs.some(r=>r.edge.id===e.id)?{...e,data:{...e.data,role:values.mode==='参考图'?'reference':refs.findIndex(r=>r.edge.id===e.id)===0?'first':'last'}}:e):current.current.edges })
  }
  function copy(ids = current.current.nodes.filter(n => n.selected).map(n => n.id)) {
    ids=[...ids,...current.current.nodes.filter(n=>n.parentId&&ids.includes(n.parentId)).map(n=>n.id)]
    clipboard.current = structuredClone({ nodes: current.current.nodes.filter(n => ids.includes(n.id)), edges: current.current.edges.filter(e => ids.includes(e.source) && ids.includes(e.target)) })
    setMenu(null)
  }
  function paste(offset = { x: 40, y: 40 }) {
    if (!clipboard.current?.nodes.length) return
    const ids = Object.fromEntries(clipboard.current.nodes.map(n => [n.id, uid()]))
    commit({ ...current.current, nodes: [...current.current.nodes.map(n => ({ ...n, selected: false })), ...clipboard.current.nodes.map(n => ({ ...structuredClone(n), id: ids[n.id], parentId:n.parentId?ids[n.parentId]:undefined, selected: true, position: { x: n.position.x + (n.parentId&&ids[n.parentId]?0:offset.x), y: n.position.y + (n.parentId&&ids[n.parentId]?0:offset.y) }, data: { ...n.data, job_id: undefined } }))],
      edges: [...current.current.edges, ...clipboard.current.edges.map(e => ({ ...structuredClone(e), id: uid(), source: ids[e.source], target: ids[e.target] }))] })
    setMenu(null)
  }
  function remove(ids = current.current.nodes.filter(n => n.selected).map(n => n.id)) {
    ids=[...ids,...current.current.nodes.filter(n=>n.parentId&&ids.includes(n.parentId)).map(n=>n.id)]
    commit({ ...current.current, nodes: current.current.nodes.filter(n => !ids.includes(n.id)), edges: current.current.edges.filter(e => !ids.includes(e.source) && !ids.includes(e.target) && !e.selected) }); setMenu(null)
  }
  function group(ungroup=false){
    const selected=current.current.nodes.filter(n=>n.selected)
    if(ungroup){
      const groups=selected.filter(n=>n.data.kind==='group')
      if(!groups.length)return
      commit({...current.current,nodes:current.current.nodes.filter(n=>!groups.some(g=>g.id===n.id)).map(n=>{
        const parent=groups.find(g=>g.id===n.parentId)
        return parent?{...n,parentId:undefined,extent:undefined,position:{x:n.position.x+parent.position.x,y:n.position.y+parent.position.y}}:n
      })});return
    }
    const members=selected.filter(n=>!n.parentId&&n.data.kind!=='group')
    if(members.length<2)return fail('请选择至少两个未分组的节点')
    const bounds=flow.getNodesBounds(members),position={x:bounds.x-32,y:bounds.y-64}
    const node=newNode('group',position);node.style={width:bounds.width+64,height:bounds.height+96};node.zIndex=-1;node.selected=true
    commit({...current.current,nodes:[node,...current.current.nodes.map(n=>members.some(m=>m.id===n.id)?{...n,parentId:node.id,position:{x:n.position.x-position.x,y:n.position.y-position.y},selected:false}:{...n,selected:false})]})
  }
  function connect(c: Connection) {
    if (c.source === c.target) return fail('节点不能引用自身')
    const source = current.current.nodes.find(n => n.id === c.source), target = current.current.nodes.find(n => n.id === c.target)
    if (!source || !target || ['upload', 'text','group'].includes(target.data.kind)||source.data.kind==='group') return fail('请连接到图片、视频或超分节点的输入端')
    const kind = outputKind(source.id, current.current.nodes, current.current.edges)
    if (kind === 'audio' || (kind === 'video' && target.data.kind !== 'upscale')) return fail('视频可连接超分节点；图片和视频生成使用图片或文本参考')
    if (target.data.kind === 'upscale' && (kind === 'text' || current.current.edges.some(e=>e.target===target.id))) return fail('超分节点只需连接一张图片或一段视频')
    const visited = new Set<string>(), reaches = (id: string): boolean => {
      if (id === c.source) return true
      if (visited.has(id)) return false
      visited.add(id); return current.current.edges.filter(e => e.source === id).some(e => reaches(e.target))
    }
    if (reaches(c.target)) return fail('不能创建循环参考')
    if (current.current.edges.some(e => e.source === c.source && e.target === c.target)) return
    const next = attachReference(current.current, {...c,id:uid()}, source, target)
    const count = referencesFor(target.id, next).filter(r=>r.node.data.kind!=='text').length
    if (target.data.kind === 'video' && count > (target.data.mode==='参考图'?9:2)) return fail('首尾帧最多两张图片；使用多张主体参考请先选择参考图模式')
    if (target.data.kind === 'video' && target.data.mode==='首尾循环' && count>1) return fail('首尾循环使用一张相同的首尾图片')
    if (target.data.kind === 'image' && count > (target.data.model==='moss-image'?4:10)) return fail('已达到此模型的参考图片上限')
    commit(next)
  }
  async function uploadFiles(files: FileList | File[], point = center()) {
    setUploading(true)
    try {
      const created: CreativeNode[] = []
      for (const [i, source] of Array.from(files).entries()) {
        const asset = await upload(source)
        if (uploadTarget.current && i === 0) {
          const target = current.current.nodes.find(n => n.id === uploadTarget.current)
          if (target?.data.kind === 'upload') update(target.id, { asset_id: asset.id, media_type: asset.media_type, width:asset.width, height:asset.height, label: asset.name.slice(0,100) })
          else {
            const node = newNode('upload', { x: (target?.position.x ?? point.x) - 380, y: target?.position.y ?? point.y })
            Object.assign(node.data, { asset_id: asset.id, media_type: asset.media_type, label: asset.name.slice(0,100) })
            commit({ ...current.current, nodes: [...current.current.nodes, node] })
            connect({source:node.id,target:uploadTarget.current,sourceHandle:'output',targetHandle:'input'})
          }
        } else {
          const node = newNode('upload', { x: point.x + i * 320, y: point.y }); Object.assign(node.data, { asset_id: asset.id, media_type: asset.media_type, label: asset.name.slice(0,100) }); created.push(node)
        }
      }
      if (created.length) commit({ ...current.current, nodes: [...current.current.nodes, ...created] })
    } catch (error) { fail((error as Error).message) } finally { setUploading(false); uploadTarget.current = null }
  }
  const keyActions = useRef<(event: KeyboardEvent) => void>(() => {})
  keyActions.current = e => {
    if (document.querySelector('[role="dialog"]')) return
    if (e.key === 'Escape') { setAdding(null); setMenu(null); setSearch(null); setPreview(null); return }
    if ((e.target as HTMLElement).closest('input,textarea,select,[contenteditable="true"]')) return
    const ctrl = e.ctrlKey || e.metaKey, key = e.key.toLowerCase()
    if (ctrl && key === 'z') { e.preventDefault(); undo(e.shiftKey); return }
    if (ctrl && key === 'a') { e.preventDefault(); commit({ ...current.current, nodes: current.current.nodes.map(n => ({ ...n, selected: true })) }, false); return }
    if (ctrl && key === 'c') { e.preventDefault(); copy(); return }
    if (ctrl && key === 'v') { e.preventDefault(); paste(); return }
    if (ctrl && key === 'd') { e.preventDefault(); copy(); paste(); return }
    if (ctrl && key === 'g') { e.preventDefault(); group(e.shiftKey);return }
    if (key === 'delete' || key === 'backspace') { e.preventDefault(); remove(); return }
    if (!ctrl && !e.altKey && ['t', 'i', 'v', 'u'].includes(key)) { e.preventDefault(); add(({ t:'text', i:'image', v:'video', u:'upload' } as Record<string, Kind>)[key]) }
  }
  useEffect(() => { const handler = (e: KeyboardEvent) => keyActions.current(e); window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler) }, [])
  function onDrop(e: DragEvent) { e.preventDefault(); uploadTarget.current = null; uploadFiles(e.dataTransfer.files, flow.screenToFlowPosition({ x:e.clientX, y:e.clientY })) }
  const shown = current.current.nodes.find(n => n.id === preview?.id)
  return <Surface className="cc-canvas" ref={wrapper} onDragOver={e => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy' }} onDrop={onDrop}>
    <CardContext.Provider value={{ previewOpen:!!preview, videoUpscale:id=>upscaleInputIsVideo(id,canvas.nodes,canvas.edges), update, references: id => referencesFor(id,canvas), generate, upload: id => { uploadTarget.current = id; file.current?.click() }, preview: (id,versionId)=>setPreview({id,versionId}),
      job:id=>jobs.filter(j=>Object.values(j.creative?.output_nodes??{}).includes(id)).sort((a,b)=>b.created_at-a.created_at)[0], jobAction,
      referenceRole:(id,source)=>String(canvas.edges.find(e=>e.target===id&&e.source===source)?.data?.role??'reference'),
      removeReference:(id,source)=>commit({...current.current,edges:current.current.edges.filter(e=>!(e.target===id&&e.source===source))}),
      setReferenceRole:(id,source,role)=>commit(setReferenceRole(current.current,id,source,role)) }}>
      <ReactFlow<CreativeNode> nodes={canvas.nodes.map(n => ({ ...n, draggable: true }))} edges={canvas.edges} nodeTypes={nodeTypes} defaultViewport={canvas.viewport} minZoom={.15} maxZoom={2.5} deleteKeyCode={null} nodeDragThreshold={5}
        onNodesChange={changes => commit({ ...current.current, nodes: applyNodeChanges(changes, current.current.nodes) }, false)}
        onEdgesChange={changes => commit({ ...current.current, edges: applyEdgeChanges(changes, current.current.edges) }, changes.some(c => c.type === 'remove'))}
        onNodeDragStart={() => { history.current.past.push(structuredClone(current.current)); history.current.future = [] }}
        onConnect={connect} onMove={(_, viewport) => setZoom(viewport.zoom)} onMoveEnd={(_, viewport) => commit({ ...current.current, viewport }, false)}
        onPaneClick={() => { setAdding(null); setMenu(null); setSearch(null) }}
        onPaneContextMenu={e => { e.preventDefault(); const rect = wrapper.current!.getBoundingClientRect(); setMenu({ x: Math.min(e.clientX - rect.left,rect.width-244), y: Math.min(e.clientY - rect.top,rect.height-240), point: flow.screenToFlowPosition({ x:e.clientX,y:e.clientY }) }); setAdding(null) }}
        onNodeContextMenu={(e,n) => { e.preventDefault(); const rect=wrapper.current!.getBoundingClientRect(); setMenu({ x:Math.min(e.clientX-rect.left,rect.width-244), y:Math.min(e.clientY-rect.top,rect.height-300), node:n.id, point:n.position }) }}
        onDoubleClick={e => { if ((e.target as HTMLElement).closest('.react-flow__node,.cc-canvas-toolbar,.cc-popover')) return; const rect=wrapper.current!.getBoundingClientRect(); setAdding({ x:Math.min(e.clientX-rect.left,rect.width-250), y:Math.min(e.clientY-rect.top,rect.height-280), point:flow.screenToFlowPosition({x:e.clientX,y:e.clientY}) }) }}
        panOnDrag={hand ? true : [1,2]} selectionOnDrag={!hand} selectionMode={SelectionMode.Partial} multiSelectionKeyCode="Shift" panActivationKeyCode="Space" zoomOnDoubleClick={false}
        defaultEdgeOptions={{ type:'default', style:{stroke:'#626262',strokeWidth:1.5}, interactionWidth:20 }} proOptions={{hideAttribution:true}}>
        <Background color="rgba(255,255,255,.3)" gap={22.4} size={1.12} /><Presence>{minimap && <Surface className="cc-minimap-wrap"><MiniMap position="bottom-left" pannable zoomable nodeColor="#454545" maskColor="#1119" /></Surface>}</Presence>
      </ReactFlow>
    </CardContext.Provider>
    <div className="cc-canvas-toolbar">{canvas.nodes.some(n=>['image','video','upscale'].includes(n.data.kind))&&<button onClick={()=>{const selected=canvas.nodes.filter(n=>n.selected&&['image','video','upscale'].includes(n.data.kind));const targets=(selected.length?selected:canvas.nodes.filter(n=>['image','video','upscale'].includes(n.data.kind))).map(n=>n.id);generate(targets)}}>生成{canvas.nodes.some(n=>n.selected&&['image','video','upscale'].includes(n.data.kind))?'所选':'全部'}</button>}<button onClick={() => flow.fitView({padding:.3,duration:220})}>原始画布</button><div><button aria-label="节点搜索" onClick={() => setSearch(search === null ? '' : null)}><Search size={17} /></button><button aria-label="适应画布" onClick={() => flow.fitView({padding:.25,duration:200})}><Maximize2 size={17} /></button></div></div>
    {canvas.nodes.length === 0 && <div className="cc-empty-canvas"><div className="cc-empty-icons"><Image size={30} /><kindIcons.video size={30} /><kindIcons.text size={30} /></div><h1>说说你想做的</h1><p>想自己摆？ <kbd>双击</kbd> 或 <kbd>右键</kbd> 画布新建卡片，也可以把文件拖进来</p></div>}
    <div className="cc-canvas-bottom"><div className="cc-navigation"><button title="选择 (Shift 多选)" className={!hand?'active':''} onClick={() => setHand(false)}><MousePointer2 size={18} /></button><button title="移动画布 (Space)" className={hand?'active':''} onClick={() => setHand(true)}><Hand size={18} /></button><i /><button title="撤销 Ctrl+Z" onClick={() => undo()}><Undo2 size={17} /></button><button title="重做 Ctrl+Shift+Z" onClick={() => undo(true)}><Redo2 size={17} /></button></div><button className="cc-add-main" aria-label="添加节点" onClick={() => setAdding(adding ? null : {x:Math.max(12,(wrapper.current?.clientWidth ?? 900)/2-117),y:(wrapper.current?.clientHeight ?? 800)-330})}>{adding ? <X size={22} /> : <Plus size={22} />}</button></div>
    <div className="cc-canvas-corner"><button title="缩小" onClick={() => flow.zoomOut()}><ZoomOut size={16} /></button><span>{Math.round(zoom*100)}%</span><button title="放大" onClick={() => flow.zoomIn()}><ZoomIn size={16} /></button><i /><button aria-label="打开小地图" aria-pressed={minimap} onClick={() => setMinimap(!minimap)}><Map size={17} /></button></div>
    <Presence>{adding && <Surface className="cc-popover cc-add-menu" style={{left:adding.x,top:adding.y}}><small>添加节点</small>{nodeKinds.map(kind => { const Icon=kindIcons[kind]; return <button key={kind} onClick={() => add(kind)}><Icon size={18} /><span>{names[kind]}<small>{descriptions[kind]}</small></span><kbd>{shortcuts[kind]}</kbd></button> })}</Surface>}</Presence>
    <Presence>{menu && <Surface className="cc-popover cc-context" style={{left:menu.x,top:menu.y}}>{menu.node ? <><button onClick={() => { copy([menu.node!]); paste() }}><Copy size={16} />副本<kbd>Ctrl D</kbd></button><button onClick={() => copy([menu.node!])}><Copy size={16} />复制<kbd>Ctrl C</kbd></button><hr /><button className="danger" onClick={() => remove([menu.node!])}><Trash2 size={16} />删除<kbd>Del</kbd></button></> : <><button onClick={() => add('upload',menu.point)}><Upload size={16} />上传<kbd>U</kbd></button><button onClick={() => {setAdding({...menu});setMenu(null)}}><Plus size={16} />添加节点</button><hr /><button onClick={() => {undo();setMenu(null)}}><Undo2 size={16} />撤销<kbd>Ctrl Z</kbd></button><button onClick={() => {undo(true);setMenu(null)}}><Redo2 size={16} />重做<kbd>Ctrl Shift Z</kbd></button><hr /><button onClick={() => paste()}><Copy size={16} />粘贴<kbd>Ctrl V</kbd></button></>}</Surface>}</Presence>
    <Presence>{search !== null && <Surface className="cc-popover cc-search"><label><Search size={17} /><input autoFocus aria-label="搜索节点" placeholder="搜索画布中的节点" value={search} onChange={e=>setSearch(e.target.value)} /><button title="关闭搜索" onClick={()=>setSearch(null)}><X size={16}/></button></label>{canvas.nodes.filter(n=>n.data.label.includes(search)||n.data.prompt.includes(search)).map(n=><button key={n.id} onClick={()=>{flow.setCenter(n.position.x+140,n.position.y+140,{zoom:1,duration:250});commit({...current.current,nodes:current.current.nodes.map(x=>({...x,selected:x.id===n.id}))},false);setSearch(null)}}><ListFilter size={15}/>{n.data.label}</button>)}</Surface>}</Presence>
    <Presence>{uploading && <Surface className="cc-uploading" role="status">正在保存素材…</Surface>}</Presence>
    <Presence>{shown?.data.asset_id && <NodeDetails key={shown.id} node={shown} references={referencesFor(shown.id,canvas)} setRole={(source,role)=>commit(setReferenceRole(current.current,shown.id,source,role))} removeReference={source=>commit({...current.current,edges:current.current.edges.filter(e=>!(e.target===shown.id&&e.source===source))})} videoUpscale={upscaleInputIsVideo(shown.id,canvas.nodes,canvas.edges)} initialVersion={preview?.versionId} update={values=>update(shown.id,values)} generate={()=>generate(shown.id)} close={()=>setPreview(null)} job={jobs.filter(j=>Object.values(j.creative?.output_nodes??{}).includes(shown.id)).sort((a,b)=>b.created_at-a.created_at)[0]}/>}</Presence>
    <input ref={file} hidden type="file" multiple accept="image/png,image/jpeg,image/webp,video/mp4,video/webm,audio/mpeg,audio/wav" onChange={e=>{if(e.target.files)uploadFiles(e.target.files);e.target.value=''}}/>
  </Surface>
}
export default function Canvas(props: Parameters<typeof CanvasInner>[0]) { return <ReactFlowProvider><CanvasInner {...props}/></ReactFlowProvider> }
