import GlassSelect from './GlassSelect'
import { motion, MotionConfig } from 'motion/react'
import { Presence, Surface, transition, useDismiss } from './Motion'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, Check, ChevronDown, ChevronRight, Cloud, Copy, Folder, FolderPlus, Grid2X2, Hash, Home, Image, LayoutGrid, List, LoaderCircle, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Pencil, Plus, Search, Trash2, Undo2, X } from 'lucide-react'
import Canvas from './Canvas'
import { mergeCompleted } from './versions'
import AssetLibrary from './AssetLibrary'
import { MiniCompute, ComputePanel } from './Compute'
import { api, assetUrl, cleanCanvas, setToken, uid, type Asset, type CanvasData, type Project, type ProjectSummary } from './types'
import { type State } from '../types'
import './creative.css'

type DialogState = { title: string; value: string; submit: (value: string) => Promise<void> }
const contentKey = (p: Project) => JSON.stringify({ name:p.name, folder_id:p.folder_id, canvases:p.canvases.map(cleanCanvas) })
const relativeTime = (t: number) => { const mins=Math.floor((Date.now()/1000-t)/60); return mins<1?'几秒前':mins<60?`${mins} 分钟前`:mins<1440?`${Math.floor(mins/60)} 小时前`:`${Math.floor(mins/1440)} 天前` }

export default function CreativeApp() {
  const [state,setState]=useState<State|null>(null), [items,setItems]=useState<ProjectSummary[]>([]), [project,setProject]=useState<Project|null>(null)
  const [canvasId,setCanvasId]=useState(''), [folderId,setFolderId]=useState<string|null>(null), [trash,setTrash]=useState(false)
  const [query,setQuery]=useState(''), [view,setView]=useState<'grid'|'list'>('grid'), [filter,setFilter]=useState('全部'), [collapsed,setCollapsed]=useState(false)
  const [error,setError]=useState(''), [notice,setNotice]=useState(''), [saveStatus,setSaveStatus]=useState('已保存'), [busy,setBusy]=useState(false)
  const [dialog,setDialog]=useState<DialogState|null>(null), [moveItem,setMoveItem]=useState<string|null>(null), [moveFolder,setMoveFolder]=useState<string|null>(null)
  const [menu,setMenu]=useState<{id:string;x:number;y:number}|null>(null), [compute,setCompute]=useState(false)
  const [account,setAccount]=useState(localStorage.getItem('canvas-account')||'colab1'), [canvasMenu,setCanvasMenu]=useState<string|null>(null)
  const [addRequest,setAddRequest]=useState(0)
  const [library,setLibrary]=useState(false),[assetRequest,setAssetRequest]=useState<(Asset&{requestId:string})|null>(null)
  const [selectedProjects,setSelectedProjects]=useState<string[]>([])
  useDismiss(!!dialog||!!moveItem,()=>{setDialog(null);setMoveItem(null)})
  useDismiss(!!menu,()=>setMenu(null))
  const toggleProject=(id:string)=>setSelectedProjects(ids=>ids.includes(id)?ids.filter(x=>x!==id):[...ids,id])
  const current=useRef(project); current.current=project
  const saved=useRef<Record<string,string>>({}), revisions=useRef<Record<string,number>>({}), saveChain=useRef<Promise<void>>(Promise.resolve())
  const fetchItems=useCallback(async()=>setItems(await api<ProjectSummary[]>('/projects')),[])
  const refresh=useCallback(async()=>{const next=await api<State>('/state');setToken(next.token);setState(next)},[])
  const fail=useCallback((message:string)=>setError(message),[])

  const persist=useCallback(async(p:Project)=>{
    const snapshot={...p,canvases:p.canvases.map(cleanCanvas)}, key=contentKey(snapshot)
    const operation=saveChain.current.catch(()=>{}).then(async()=>{
      if(saved.current[p.id]===key)return
      setSaveStatus('保存中…')
      try {
        const result=await api<Project>('/projects/'+p.id,'PUT',{...snapshot,revision:revisions.current[p.id]??p.revision})
        revisions.current[p.id]=result.revision;saved.current[p.id]=key
        if(current.current?.id===p.id)setSaveStatus(contentKey(current.current)===key?'已保存':'保存中…')
      }catch(e){setSaveStatus('保存失败');setError((e as Error).message);throw e}
    });saveChain.current=operation;await operation
  },[])
  async function flushCurrent() {
    const id=current.current?.id
    while(current.current?.id===id && current.current && saved.current[id!]!==contentKey(current.current)) await persist(current.current)
  }
  useEffect(()=>{let alive=true;Promise.all([refresh(),fetchItems()]).then(async()=>{
    const id=location.hash.match(/^#project=([\w-]+)/)?.[1]
    if(id&&alive){const p=await api<Project>('/projects/'+id);revisions.current[p.id]=p.revision;saved.current[p.id]=contentKey(p);setProject(p);setCanvasId(p.canvases[0]?.id??'')}
  }).catch(e=>setError(e.message));const timer=setInterval(()=>refresh().catch(()=>setError('本地服务连接中断，请检查启动窗口')),3000);return()=>{alive=false;clearInterval(timer)}},[refresh,fetchItems])
  useEffect(()=>{if(!project||saved.current[project.id]===contentKey(project))return;setSaveStatus('保存中…');const timer=setTimeout(()=>persist(project).catch(()=>{}),600);return()=>clearTimeout(timer)},[project,persist])
  useEffect(()=>{const handler=(e:BeforeUnloadEvent)=>{if(current.current&&saved.current[current.current.id]!==contentKey(current.current)){e.preventDefault();e.returnValue=''}};window.addEventListener('beforeunload',handler);return()=>window.removeEventListener('beforeunload',handler)},[])
  useEffect(()=>{if(!notice)return;const timer=setTimeout(()=>setNotice(''),3500);return()=>clearTimeout(timer)},[notice])
  useEffect(()=>{
    if(!state||!project)return
    setProject(previous=>previous&&previous.id===project.id?mergeCompleted(previous,state.jobs):previous)
  },[state,project?.id])
  const changeCanvas=useCallback((canvas:CanvasData)=>setProject(p=>p?{...p,canvases:p.canvases.map(c=>c.id===canvas.id?canvas:c)}:p),[])
  async function action(fn:()=>Promise<void>){setBusy(true);setError('');try{await fn()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function open(item:ProjectSummary){await action(async()=>{
    if(item.kind==='folder'){setFolderId(item.id);return}
    const p=await api<Project>('/projects/'+item.id);await flushCurrent();revisions.current[p.id]=p.revision;saved.current[p.id]=contentKey(p);setProject(p);setCanvasId(p.canvases[0]?.id??'');setSaveStatus('已保存');location.hash='project='+p.id
  })}
  async function create(kind:'project'|'folder',name='Untitled'){await action(async()=>{
    await flushCurrent()
    const p=await api<Project>('/projects','POST',{kind,name,folder_id:folderId});await fetchItems()
    if(kind==='project'){await flushCurrent();revisions.current[p.id]=p.revision;saved.current[p.id]=contentKey(p);setProject(p);setCanvasId(p.canvases[0].id);location.hash='project='+p.id}
  })}
  async function back(){await action(async()=>{await flushCurrent();await fetchItems();await flushCurrent();setProject(null);setMenu(null);location.hash=''})}
  function rename(item:ProjectSummary|Project){setMenu(null);setDialog({title:item.kind==='folder'?'重命名文件夹':'重命名项目',value:item.name,submit:async name=>{
    if(current.current?.id===item.id){setProject(p=>p?{...p,name}:p)}else{await api('/projects/'+item.id,'PATCH',{name});await fetchItems()}
  }})}
  async function changeItem(id:string,body:unknown){await action(async()=>{await api('/projects/'+id,'PATCH',body);await fetchItems();setMenu(null)})}
  async function changeSelection(body:unknown){await action(async()=>{for(const id of selectedProjects)await api('/projects/'+id,'PATCH',body);setSelectedProjects([]);await fetchItems()})}
  function inheritedTrash(item:ProjectSummary){const seen=new Set<string>();let p:ProjectSummary|undefined=item;while(p){if(p.deleted_at)return true;if(!p.folder_id||seen.has(p.id))return false;seen.add(p.id);p=items.find(x=>x.id===p!.folder_id)}return false}
  const shown=items.filter(p=>trash?!!p.deleted_at:!inheritedTrash(p)&&(query||p.folder_id===folderId)).filter(p=>p.name.toLowerCase().includes(query.toLowerCase())).filter(p=>filter==='全部'||(filter==='项目'?p.kind==='project':p.kind==='folder'))
  const canvas=project?.canvases.find(c=>c.id===canvasId)??project?.canvases[0]
  const profile=state?.accounts.find(a=>a.id===account), machine=profile?.machine
  async function generate(id:string|string[]){if(!current.current||!canvas)return;const source=current.current,sourceCanvas=canvas.id,sourceAccount=account;await action(async()=>{await persist(source);await api(Array.isArray(id)?`/projects/${source.id}/canvases/${sourceCanvas}/generate`:`/projects/${source.id}/canvases/${sourceCanvas}/nodes/${id}/generate`,'POST',{account:sourceAccount,stop_after:false,...(Array.isArray(id)?{nodes:id}:{})});await refresh();setNotice('任务已加入队列，环境会自动准备')})}
  function newCanvas(){if(!project)return;const c:CanvasData={id:uid(),name:'Untitled',nodes:[],edges:[],viewport:{x:0,y:0,zoom:.7142857}};setProject({...project,canvases:[...project.canvases,c]});setCanvasId(c.id)}
  function renameCanvas(c:CanvasData){setCanvasMenu(null);setDialog({title:'重命名画布',value:c.name,submit:async name=>{setProject(p=>p?{...p,canvases:p.canvases.map(x=>x.id===c.id?{...x,name}:x)}:p)}})}
  return <MotionConfig reducedMotion="user"><div className={`creative-app ${project?'cc-project-open':''}`} onClick={()=>menu&&setMenu(null)}>
    {state?.simulated&&<div className="cc-simulation-badge">界面测试 · 模拟算力</div>}
    <Presence>{(!project||collapsed)&&<Surface className="cc-floating-monitor"><MiniCompute account={profile} open={()=>setCompute(true)} refresh={refresh}/></Surface>}</Presence>
    <Presence mode="wait">{!project ? <Surface tone="fade" key="home" className="cc-home-page">
      <header className="cc-home-header"><a className="cc-wordmark" href="#" onClick={e=>{e.preventDefault();setFolderId(null);setTrash(false)}}><span className="cc-logo"/>Canvas</a><nav><button onClick={()=>{setFolderId(null);setTrash(false)}}><Home size={18}/>主页</button><button className="active" onClick={()=>{setFolderId(null);setTrash(false)}}><LayoutGrid size={18}/>工作空间</button><button onClick={()=>setLibrary(true)}><Image size={18}/>素材库</button></nav><button className="cc-account-button" onClick={()=>setCompute(true)}><span className={`cc-status-dot ${machine?.phase==='ready'?'ready':''}`}/><Cloud size={17}/>{profile?.label??'Colab'}<ChevronDown size={14}/></button></header>
      <main className="cc-projects"><div className="cc-projects-toolbar"><div className="cc-project-tabs"><button className={!trash?'active':''} onClick={()=>setTrash(false)}>个人</button><button className={trash?'active':''} onClick={()=>setTrash(true)}>回收站</button></div><div className="cc-project-actions"><label className="cc-searchbox"><Search size={18}/><input aria-label="搜索项目" placeholder="搜索" value={query} onChange={e=>setQuery(e.target.value)}/></label><GlassSelect label="项目类型" value={filter} change={setFilter} options={[{value:"全部",label:"显示全部"},{value:"项目",label:"项目"},{value:"文件夹",label:"文件夹"}]}/><div className="cc-view-switch"><button aria-label="Grid view" className={view==='grid'?'active':''} onClick={()=>setView('grid')}><Grid2X2 size={18}/></button><button aria-label="List view" className={view==='list'?'active':''} onClick={()=>setView('list')}><List size={18}/></button></div><i/><button className="cc-folder-add" aria-label="新建文件夹" disabled={trash} onClick={()=>setDialog({title:'新建文件夹',value:'',submit:async name=>create('folder',name)})}><FolderPlus size={20}/></button><button className="cc-white-button" disabled={trash||busy} onClick={()=>create('project')}><Plus size={19}/>新建项目</button></div></div>
      {folderId&&!query&&!trash&&<div className="cc-breadcrumb"><button onClick={()=>setFolderId(items.find(i=>i.id===folderId)?.folder_id??null)}><ArrowLeft size={16}/>返回</button><ChevronRight size={14}/><span>{items.find(i=>i.id===folderId)?.name}</span></div>}
      <Presence>{!!selectedProjects.length&&<Surface className="cc-project-selection"><span>已选择 {selectedProjects.length} 项</span><button onClick={()=>setSelectedProjects(shown.map(p=>p.id))}>全选</button><button onClick={()=>{setMoveItem('selection');setMoveFolder(null)}}>移动至…</button><button onClick={()=>changeSelection({deleted:!trash})}>{trash?'恢复':'删除'}</button><button onClick={()=>setSelectedProjects([])}>取消选择</button></Surface>}</Presence>
      <div className={`cc-project-grid cc-view-${view}`}>
        {!trash&&!query&&<button className="cc-new-project" onClick={()=>create('project')}><span><Plus size={24}/></span><strong>新建项目</strong></button>}
        {shown.map(item=><article className={`cc-project-tile ${item.kind==='folder'?'cc-folder-tile':''} ${selectedProjects.includes(item.id)?'cc-tile-selected':''}`} key={item.id} tabIndex={0} onDoubleClick={()=>!selectedProjects.length&&open(item)} onKeyDown={e=>{if(e.key==='Enter')selectedProjects.length?toggleProject(item.id):open(item)}}>
          <button className="cc-project-thumbnail" aria-label={'打开 '+item.name} onClick={e=>selectedProjects.length||e.shiftKey||e.ctrlKey?toggleProject(item.id):open(item)}>{item.thumbnail_asset?(item.thumbnail_type?.startsWith('video/')?<video muted preload="metadata" src={assetUrl(item.thumbnail_asset)}/>:item.thumbnail_type?.startsWith('audio/')?<span>音频素材</span>:<img src={assetUrl(item.thumbnail_asset)} alt=""/>):item.kind==='folder'?<Folder size={82} strokeWidth={.7}/>:<div className="cc-blank-thumbnail"><LayoutGrid size={34} strokeWidth={.7}/></div>}</button>
          <button className="cc-project-check" aria-label={'选择 '+item.name} aria-pressed={selectedProjects.includes(item.id)} onClick={()=>toggleProject(item.id)}>{selectedProjects.includes(item.id)&&<Check size={15}/>}</button>
          <div className="cc-tile-title"><button onClick={()=>open(item)}>{item.name}</button><button aria-label={'重命名 '+item.name} onClick={()=>rename(item)}><Pencil size={13}/></button></div><small>编辑于 {relativeTime(item.updated_at)}{item.kind==='folder'&&<span>{items.filter(i=>i.folder_id===item.id&&!i.deleted_at).length} 个项目</span>}</small>
          <button className="cc-tile-more" aria-label={item.name+' 的更多操作'} onClick={e=>{e.stopPropagation();setMenu({id:item.id,x:Math.min(e.clientX-120,window.innerWidth-200),y:e.clientY+12})}}><MoreHorizontal size={18}/></button>
        </article>)}
      </div>{!shown.length&&(query||trash)&&<div className="cc-no-projects">{trash?'回收站为空':'没有找到匹配的项目'}</div>}</main>
    </Surface> : <Surface tone="fade" key={project.id} className={`cc-project-layout ${collapsed?'cc-sidebar-collapsed':''}`}>
      <aside className="cc-sidebar"><div className="cc-sidebar-title"><button title="返回工作区" onClick={back}><span className="cc-logo"/></button><button className="cc-project-name" onClick={()=>rename(project)}>{project.name}</button><button aria-label="收起侧栏" onClick={()=>setCollapsed(true)}><PanelLeftClose size={16}/></button></div>
        <button className="cc-side-link" onClick={()=>setAddRequest(v=>v+1)}><Plus size={18}/>添加节点</button><button className="cc-side-link" onClick={()=>setDialog({title:'搜索画布',value:'',submit:async value=>{const c=project.canvases.find(c=>c.name.includes(value)||c.nodes.some(n=>n.data.label.includes(value)));if(c)setCanvasId(c.id);else setNotice('没有找到匹配的画布')}})}><Search size={18}/>搜索</button><button className="cc-side-link" onClick={()=>setLibrary(true)}><Image size={18}/>素材库</button>
        <div className="cc-sidebar-section"><span>画布</span><button aria-label="新建画布" onClick={newCanvas}><Plus size={17}/></button></div>
        <div className="cc-canvas-list">{project.canvases.map(c=><div className={c.id===canvas?.id?'active':''} key={c.id}><button className="cc-canvas-select" onClick={()=>{setCanvasId(c.id);setCanvasMenu(null)}}><Hash size={17}/><span>{c.name}</span></button><button className="cc-canvas-more" aria-label={c.name+' 的画布操作'} onClick={()=>setCanvasMenu(canvasMenu===c.id?null:c.id)}><MoreHorizontal size={15}/></button><Presence>{canvasMenu===c.id&&<Surface className="cc-popover cc-canvas-list-menu"><button onClick={()=>renameCanvas(c)}><Pencil size={14}/>重命名</button><button onClick={()=>{const clone=structuredClone(c);clone.id=uid();clone.name+=' 副本';setProject({...project,canvases:[...project.canvases,clone]});setCanvasId(clone.id);setCanvasMenu(null)}}><Copy size={14}/>副本</button><button disabled={project.canvases.length===1} onClick={()=>{setProject({...project,canvases:project.canvases.filter(x=>x.id!==c.id)});setCanvasMenu(null)}}><Trash2 size={14}/>删除画布</button></Surface>}</Presence></div>)}</div>
        <div className="cc-sidebar-save"><Check size={13}/>{saveStatus}</div><MiniCompute account={profile} open={()=>setCompute(true)} refresh={refresh}/>
      </aside>
      <Presence>{collapsed&&<motion.button initial={{opacity:0,scale:.98}} animate={{opacity:1,scale:1}} exit={{opacity:0,scale:.98}} transition={transition} className="cc-expand-sidebar" aria-label="展开侧栏" onClick={()=>setCollapsed(false)}><PanelLeftOpen size={18}/></motion.button>}</Presence>
      <Presence>{canvas&&<Canvas key={canvas.id} canvas={canvas} change={changeCanvas} fail={fail} generate={generate} addRequest={addRequest}
        jobs={state?.jobs.filter(j=>j.creative?.project_id===project.id&&j.creative?.canvas_id===canvas.id)??[]}
        assetRequest={assetRequest} jobAction={(id,operation)=>action(async()=>{await api(`/jobs/${id}/${operation}`,'POST');await refresh()})}/>}</Presence>
    </Surface>}</Presence>
    <Presence>{menu&&<Surface className="cc-popover cc-project-menu" style={{left:menu.x,top:menu.y}} onClick={e=>e.stopPropagation()}>{!trash?<><button onClick={()=>{open(items.find(i=>i.id===menu.id)!);setMenu(null)}}>打开</button><button onClick={()=>rename(items.find(i=>i.id===menu.id)!)}>重命名</button><button onClick={()=>{setSelectedProjects([menu.id]);setMenu(null)}}>选择</button><button disabled={items.find(i=>i.id===menu.id)?.kind==='folder'} onClick={()=>action(async()=>{await api('/projects/'+menu.id+'/duplicate','POST');await fetchItems();setMenu(null)})}>创建副本</button><hr/><button onClick={()=>{setMoveItem(menu.id);setMoveFolder(null);setMenu(null)}}>移动至…</button><hr/><button className="danger" onClick={()=>changeItem(menu.id,{deleted:true})}>删除</button></>:<button onClick={()=>changeItem(menu.id,{deleted:false})}><Undo2 size={15}/>恢复项目</button>}</Surface>}</Presence>
    <Presence>{error&&<Surface className="cc-error" role="alert"><span>{error}</span><button aria-label="关闭错误" onClick={()=>setError('')}><X size={17}/></button></Surface>}</Presence><Presence>{notice&&<Surface className="cc-toast" role="status"><Check size={16}/>{notice}</Surface>}</Presence>
    <Presence>{dialog&&<NameDialog key={dialog.title} dialog={dialog} close={()=>setDialog(null)} fail={fail}/>}</Presence>
    <Presence>{moveItem&&<Surface tone="fade" className="cc-modal-backdrop"><Surface role="dialog" aria-modal="true" aria-label="移动至" className="cc-dialog"><h2>移动至…</h2><div className="cc-folder-choices"><button className={moveFolder===null?'active':''} onClick={()=>setMoveFolder(null)}><Home size={18}/>工作空间根目录</button>{items.filter(i=>i.kind==='folder'&&!i.deleted_at&&i.id!==moveItem).map(f=><button key={f.id} className={moveFolder===f.id?'active':''} onClick={()=>setMoveFolder(f.id)}><Folder size={18}/>{f.name}</button>)}</div><footer><button onClick={()=>setMoveItem(null)}>取消</button><button className="cc-white-button" onClick={()=>action(async()=>{if(moveItem==='selection'){for(const id of selectedProjects)await api('/projects/'+id,'PATCH',{folder_id:moveFolder});setSelectedProjects([])}else await api('/projects/'+moveItem,'PATCH',{folder_id:moveFolder});await fetchItems();setMoveItem(null)})}>继续</button></footer></Surface></Surface>}</Presence>
    <Presence>{library&&<AssetLibrary close={()=>setLibrary(false)} choose={canvas?asset=>{setAssetRequest({...asset,requestId:uid()});setLibrary(false)}:undefined}/>}</Presence>
    <Presence>{compute&&<ComputePanel state={state} accountId={account} setAccount={id=>{setAccount(id);localStorage.setItem('canvas-account',id)}} refresh={refresh} close={()=>setCompute(false)}/>}</Presence>
    <Presence>{busy&&<Surface className="cc-busy"><LoaderCircle size={15}/>处理中…</Surface>}</Presence>
  </div></MotionConfig>
}

function NameDialog({dialog,close,fail}:{dialog:DialogState;close:()=>void;fail:(s:string)=>void}){
  const [value,setValue]=useState(dialog.value),[busy,setBusy]=useState(false)
  return <Surface tone="fade" className="cc-modal-backdrop"><motion.form initial={{opacity:0,scale:.98}} animate={{opacity:1,scale:1}} exit={{opacity:0,scale:.98}} transition={transition} className="cc-dialog" role="dialog" aria-modal="true" aria-label={dialog.title} onSubmit={async e=>{e.preventDefault();if(!value.trim())return;setBusy(true);try{await dialog.submit(value.trim());close()}catch(e){fail((e as Error).message)}finally{setBusy(false)}}}><h2>{dialog.title}</h2><input autoFocus aria-label="名称" placeholder="输入名称" value={value} maxLength={100} onChange={e=>setValue(e.target.value)}/><footer><button type="button" onClick={close}>取消</button><button type="submit" className="cc-white-button" disabled={!value.trim()||busy}>确定</button></footer></motion.form></Surface>
}
