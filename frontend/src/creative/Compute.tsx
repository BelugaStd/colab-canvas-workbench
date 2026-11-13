import GlassSelect from './GlassSelect'
import Disclosure from './Disclosure'
import { motion } from 'motion/react'
import { Presence, Surface, transition, useDismiss } from './Motion'
import { useEffect, useRef, useState } from 'react'
import { Activity, ChevronRight, Cpu, LoaderCircle, Power, RefreshCw, Terminal, X } from 'lucide-react'
import { activeStatuses, phaseLabel, statusLabel, type Account, type Metrics, type State } from '../types'
import { api } from './types'

const pct=(v:number|null|undefined)=>v==null?'—':Math.round(v)+'%'
const gb=(v:number|null|undefined)=>v==null?'—':(v/2**30).toFixed(1)+'G'
const number=(v:number|null|undefined,d=1)=>v==null?'—':v.toFixed(d)

export function useMetrics(account?:Account){
  const [metrics,setMetrics]=useState<Metrics|null>(null)
  useEffect(()=>{
    setMetrics(null)
    if(!account?.machine.session)return
    let alive=true
    const read=()=>api<Metrics>(`/accounts/${account.id}/metrics`).then(m=>{if(alive)setMetrics(m)}).catch(()=>{if(alive)setMetrics(null)})
    read();const timer=setInterval(read,2500)
    return()=>{alive=false;clearInterval(timer)}
  },[account?.id,account?.machine.session])
  return metrics
}

export function MiniCompute({account,open,refresh}:{account?:Account;open:()=>void;refresh:()=>Promise<void>}){
  const metrics=useMetrics(account),[now,setNow]=useState(Date.now()/1000)
  const refreshRef=useRef(refresh);refreshRef.current=refresh
  useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()/1000),1000);return()=>clearInterval(timer)},[])
  useEffect(()=>{
    if(!account?.credential_present)return
    let pending=false
    const read=async()=>{if(pending)return;pending=true;try{await api(`/accounts/${account.id}/refresh`,'POST');await refreshRef.current()}catch{}finally{pending=false}}
    read();const timer=setInterval(read,45000);return()=>clearInterval(timer)
  },[account?.id,account?.credential_present])
  const s=metrics?.status==='live'?metrics.sample:null, rate=account?.usage?.rate, balance=account?.usage?.balance
  const liveBalance=balance==null?null:Math.max(0,balance-(rate??0)*Math.max(0,now-(account?.checked_at??now))/3600)
  return <button className="cc-mini-compute" aria-label="打开服务器高级面板" onClick={open}>
    <div><span className={`cc-status-dot ${account?.machine.phase==='ready'?'ready':''}`}/><strong>{account?.label??'Colab'} · {phaseLabel[account?.machine.phase??'stopped']??'未连接'}</strong><ChevronRight size={13}/></div>
    <div className="cc-mini-cost" title="余额按最近一次官方额度与当前速率估算，每 45 秒刷新官方值"><b>{liveBalance==null?'—':'≈ '+liveBalance.toFixed(2)} <small>CU</small></b><span>{number(rate,2)} CU/h</span></div>
    <div className="cc-mini-meters"><span>GPU <b>{pct(s?.gpu?.percent)}</b></span><span>CPU <b>{pct(s?.cpu.percent)}</b></span><span>功率 <b>{number(s?.gpu?.power,0)}W</b></span><span>显存 <b>{gb(s?.gpu?.memory_used)} / {gb(s?.gpu?.memory_total)}</b></span><span>内存 <b>{gb(s?.memory.used)} / {gb(s?.memory.total)}</b></span></div>
    <div className="cc-mini-status">{s?'RTX PRO 6000 · 实时':account?.machine.message??'一键连接，自动配置模型'}</div>
  </button>
}

export function ComputePanel({state,accountId,setAccount,refresh,close}:{state:State|null;accountId:string;setAccount:(id:string)=>void;refresh:()=>Promise<void>;close:()=>void}){
  useDismiss(true,close)
  const profile=state?.accounts.find(a=>a.id===accountId),machine=profile?.machine,metrics=useMetrics(profile)
  const [tab,setTab]=useState('概览'),[logs,setLogs]=useState<{time:number;message:string}[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false)
  const [command,setCommand]=useState('nvidia-smi'),[output,setOutput]=useState(''),[running,setRunning]=useState(false)
  const jobs=state?.jobs.filter(j=>j.account===accountId)??[],s=metrics?.status==='live'?metrics.sample:null
  const refreshRef=useRef(refresh);refreshRef.current=refresh
  useEffect(()=>{let alive=true;const read=()=>api<typeof logs>(`/accounts/${accountId}/logs`).then(r=>{if(alive)setLogs(r)}).catch(()=>{});read();const timer=setInterval(read,3000);return()=>{alive=false;clearInterval(timer)}},[accountId])
  async function action(fn:()=>Promise<unknown>){setBusy(true);setError('');try{await fn();await refreshRef.current()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function run(){if(!command.trim()||running)return;setRunning(true);setError('');setOutput('$ '+command+'\n');try{const r=await api<{output:string}>(`/accounts/${accountId}/command`,'POST',{command});setOutput('$ '+command+'\n'+r.output)}catch(e){setError((e as Error).message)}finally{setRunning(false)}}
  return <Surface tone="fade" className="cc-modal-backdrop" onClick={close}><motion.section initial={{opacity:0,scale:.98}} animate={{opacity:1,scale:1}} exit={{opacity:0,scale:.98}} transition={transition} className="cc-compute-panel" role="dialog" aria-modal="true" aria-label="服务器高级面板" onClick={e=>e.stopPropagation()}>
    <header><div><h2>Colab 算力</h2><p>一键连接，自动配置 ComfyUI 与工作流，模型按需准备</p></div><button aria-label="关闭服务器面板" onClick={close}><X size={20}/></button></header>
    <div className="cc-account-tabs">{state?.accounts.map(a=><button key={a.id} className={accountId===a.id?'active':''} onClick={()=>{setAccount(a.id);setOutput('');setError('')}}>{a.label}<span className={a.credential_present?'cc-signed':''}>{a.credential_present?'已登录':'待登录'} · {phaseLabel[a.machine.phase]}</span></button>)}</div>
    <div className="cc-compute-connect"><span className={`cc-status-dot ${machine?.phase==='ready'?'ready':''}`}/><div><strong>{machine?.phase==='ready'?'RTX PRO 6000 · 就绪':phaseLabel[machine?.phase??'stopped']}</strong><p>{machine?.message}</p></div><button className="cc-white-button" disabled={!profile?.credential_present||busy||['checking','provisioning','deploying','stopping'].includes(machine?.phase??'')} onClick={()=>action(()=>api(`/accounts/${accountId}/start`,'POST'))}><Power size={15}/>{machine?.phase==='ready'?'检查完整环境':'连接服务器'}</button><button className="cc-release" disabled={!machine?.session||busy} onClick={()=>action(()=>api(`/accounts/${accountId}/stop`,'POST'))}>释放</button></div>
    {!profile?.credential_present&&<p className="cc-compute-hint">运行项目 outputs 中的「登录Colab账号{accountId.slice(-1)}.cmd」，在 Google 页面登录后点击刷新。授权信息只保存在本机。</p>}
    <nav className="cc-compute-tabs">{([['概览',Activity],['任务',Cpu],['命令行',Terminal]] as const).map(([name,Icon])=><button key={String(name)} className={tab===name?'active':''} onClick={()=>setTab(String(name))}><Icon size={15}/>{String(name)}</button>)}<button aria-label="刷新账号额度" onClick={()=>action(()=>api(`/accounts/${accountId}/refresh`,'POST'))}><RefreshCw size={14}/></button></nav>
    {error&&<p className="cc-compute-error" role="alert">{error}</p>}
    <Presence mode="wait"><Surface tone="fade" key={tab} className="cc-compute-content">{tab==='概览'?<><div className="cc-resource-grid">{[['余额',number(profile?.usage?.balance,2)+' CU'],['账号消耗',number(profile?.usage?.rate,2)+' CU/h'],['GPU 占用',pct(s?.gpu?.percent)],['CPU 占用',pct(s?.cpu.percent)],['显存',gb(s?.gpu?.memory_used)+' / '+gb(s?.gpu?.memory_total)],['内存',gb(s?.memory.used)+' / '+gb(s?.memory.total)],['GPU 功率',number(s?.gpu?.power,0)+' / '+number(s?.gpu?.power_limit,0)+' W'],['GPU 温度',number(s?.gpu?.temperature,0)+'°C'],['磁盘',gb(s?.disk.used)+' / '+gb(s?.disk.total)]].map(([label,value])=><div key={label}><small>{label}</small><strong>{value}</strong></div>)}</div><div className="cc-compute-hint"><b>调度</b> 同一 GPU 按队列串行；不同账号可并行。批量生成先执行上游，共享参考只算一次。<br/>视频固定 MiniMax H3；图片支持 MiniMax Image / MOSS Image / Qwen Edit；图片超分 Real-ESRGAN，视频超分 SeedVR2。</div><div className="cc-idle-setting"><label>空闲自动释放<GlassSelect label="空闲自动释放" value={String(state?.settings.idle_minutes??15)} change={v=>action(()=>api('/settings','PUT',{...state!.settings,idle_minutes:+v}))} options={[0,10,15,30,60].map(v=>({value:String(v),label:v?v+' 分钟':'关闭'}))}/></label><label><input type="checkbox" checked={state?.settings.stop_on_exit??true} onChange={e=>action(()=>api('/settings','PUT',{...state!.settings,stop_on_exit:e.target.checked}))}/>关闭本地服务时释放 GPU</label></div><Disclosure title="连接详情"><code>{machine?.session??'暂无会话'}</code><p>SSH {machine?.port?'已连接':'未连接'} · 监控 {metrics?.status==='live'?'实时':'暂无数据'}</p></Disclosure></>:tab==='任务'?<div className="cc-job-list">{jobs.length?jobs.map(j=><article key={j.id}><div><strong>{j.name}</strong><span>{statusLabel[j.status]}{j.progress!=null?' '+j.progress+'%':''}</span></div><p>{j.message}</p>{activeStatuses.has(j.status)&&j.status!=='downloading'&&<button onClick={()=>action(()=>api(`/jobs/${j.id}/${j.status==='recovering'?'resume':'cancel'}`,'POST'))}>{j.status==='recovering'?'恢复查询':'取消'}</button>}</article>):<p>还没有生成任务</p>}</div>:<><form className="cc-command" onSubmit={e=>{e.preventDefault();run()}}><span>$</span><input aria-label="服务器命令" value={command} onChange={e=>setCommand(e.target.value)} disabled={running} placeholder="输入远程 Linux 命令"/><button disabled={running||machine?.phase!=='ready'||!command.trim()}>{running?<LoaderCircle size={15}/>:<ChevronRight size={17}/>}</button></form><div className="cc-command-presets">{['nvidia-smi','df -h /content','tail -n 50 /content/colab-workbench/server.log'].map(c=><button key={c} onClick={()=>setCommand(c)}>{c}</button>)}</div><pre className="cc-terminal-output">{output||'连接服务器后，可在此执行远程诊断命令。'}</pre><Disclosure title="部署与运行日志" defaultOpen><pre className="cc-terminal-output">{logs.map(l=>new Date(l.time*1000).toLocaleTimeString()+' '+l.message).join('\n')||'暂无日志'}</pre></Disclosure></>}</Surface></Presence>
  </motion.section></Surface>
}
