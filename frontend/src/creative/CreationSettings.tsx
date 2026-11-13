import type { CardData } from './types'
import GlassSelect from './GlassSelect'
import { videoModel, videoUpscalers } from './upscale'
import { videoMode } from './references'
import { imageModel, imageModels } from './imageModels'

function Segments({ label, values, value, change }: { label: string; values: string[]; value?: string; change: (v: string) => void }) {
  return <div className="cc-setting"><span>{label}</span><div className="cc-segments">{values.map(v => <button type="button" className={value === v ? 'active' : ''} key={v} onClick={() => { if (value !== v) change(v) }}>{v}</button>)}</div></div>
}

export default function CreationSettings({data,update,videoUpscale=false,referenceCount=0}:{data:CardData;update:(v:Partial<CardData>)=>void;videoUpscale?:boolean;referenceCount?:number}){
  const video=data.kind==='video'
  const mode=videoMode(data,referenceCount)
  const modes:Record<string,string>={'文生视频':'只使用文字，不连接图片。','首帧':'1 张图片作为视频起点；描述接下来的动作。','首尾帧':'2 张图片分别固定起点与终点；可交换图片用途。','首尾循环':'1 张图片同时作为起点与终点；动作应回到起始状态。','参考图':'1–9 张图片参考主体与外观，不固定首尾画面。'}
  const moss=data.model==='moss-image'
  const qwen=data.model==='qwen-edit'
  const imageMode=data.image_mode??(referenceCount?'edit':'text')
  return <>
        {video && <><Segments label="参考模式" values={['文生视频','首帧','首尾帧','首尾循环','参考图']} value={mode} change={mode => update({ mode })} /><p className="cc-setting-hint">{modes[mode]}</p></>}
        {data.kind === 'image' && <><Segments label="模型" values={imageModels.map(m=>m.label)} value={imageModel(data.model).label} change={v => {
          const model=imageModels.find(m=>m.label===v)!
          update({model:model.value,resolution:model.value==='qwen-edit'?'1MP':model.value==='moss-image'?(data.resolution==='1K'||data.resolution==='1MP'?'1MP':'2MP'):(data.resolution==='1MP'?'1K':'2K'),...(model.value==='qwen-edit'&&imageMode==='text'?{image_mode:'edit'}:{})})
        }} />
          <div className="cc-setting"><span>创作方式</span><GlassSelect label="图片创作方式" value={imageMode} change={image_mode=>update({image_mode})} options={[...(!qwen?[{value:'text',label:'文生图 · 无图片输入'}]:[]),{value:'edit',label:'编辑主图 · 保留未要求修改的内容'},{value:'reference',label:'参考创作 · 根据图片重新构图'}]}/></div>
          <p className="cc-setting-hint">{imageMode==='edit'?'第一张为主图；用 @图编号 指定其它图片的用途。':imageMode==='reference'?'参考图提供主体、风格等条件；请在提示词中说明各图用途。':'描述要生成的画面。连接图片后可使用编辑或参考创作。'}</p></>}
        {data.kind==='image'&&moss&&referenceCount>0&&<p className="cc-setting-hint">MOSS 编辑适配器仍属实验功能；跨风格或多图保真要求高时，建议选择 MiniMax Image。</p>}
        {data.kind==='image'&&qwen&&<p className="cc-setting-hint">Qwen Image Edit 2511 · 连接 1–3 张图片，第一张为主图。使用推荐 1MP；自选画幅会居中裁切主图，跟随主图可保留构图。</p>}
        {data.kind === 'upscale' ? <>
          {videoUpscale && <div className="cc-setting cc-upscale-model"><span>视频超分模型</span><GlassSelect label="视频超分模型" value={videoModel(data.model).value} options={videoUpscalers} change={model=>update({model})}/><p>{videoModel(data.model).hint}</p></div>}
          <Segments label="放大倍数" values={['2×', '4×']} value={['2×','4×'].includes(data.resolution??'')?data.resolution:'2×'} change={resolution => update({ resolution })} />
          <p className="cc-setting-hint">{videoUpscale?'最高 4K · 保持画幅、帧率和声音 · 自动分段处理':'连接图片或视频，自动选择对应超分流程'}</p>
        </> : <>
          <div className="cc-setting"><span>画幅</span><GlassSelect label="画幅" value={data.aspect??'source'} change={aspect=>update({aspect})} options={[{value:'source',label:referenceCount?'跟随主图':'自动 · '+(video?'16:9':'1:1')},...['16:9','3:2','4:3','1:1','3:4','9:16'].map(v=>({value:v,label:v}))]}/></div>
          {video&&referenceCount>0&&mode!=='参考图'&&<p className="cc-setting-hint">首尾图片居中裁切至输出画幅；选择「跟随主图」可保留主图构图。</p>}
          <Segments label="分辨率" values={video ? ['512p', '768p'] : imageModel(data.model).resolutions} value={moss?({'1K':'1MP','2K':'2MP'}[data.resolution??'']??data.resolution):data.resolution} change={resolution => update({ resolution })} />
          {!video&&!moss&&!qwen&&data.resolution==='1K'&&<p className="cc-setting-hint">1K 仅用于快速预览；MiniMax Image 官方有效生成范围约 4–20MP，建议至少选择 2K（约 4MP）。</p>}
          {!video&&!moss&&!qwen&&<p className="cc-setting-hint">官方全量 BF16 · 未量化、未裁剪，首次需下载约 119GB。{data.resolution==='4K'?'原生 4K 约 16MP：方图 4096×4096，尺寸随画幅变化，显存和耗时较高。':'支持原生 4K，官方推荐约 16MP；2K 可减少显存和耗时。'}</p>}
          {video ? <><div className="cc-setting"><span>时长 <b>{data.duration}s</b></span><input aria-label="时长" type="range" min={5} max={15} step={1} value={data.duration} onChange={e => update({ duration: +e.target.value })} /><div className="cc-range-labels"><span>5s</span><span>15s</span></div></div><Segments label="帧率" values={['24', '30']} value={String(data.fps)} change={v => update({ fps: +v })} />{data.fps===30&&<p className="cc-setting-hint">H3 原生生成 24 fps；30 fps 为导出转换。</p>}<div className="cc-setting cc-switch-row"><span>声音</span><button role="switch" aria-label="声音" aria-checked={!!data.audio} className="cc-switch" onClick={() => update({ audio: !data.audio })}><i /></button></div></> : <Segments label="质量" values={['标准', '精细']} value={data.quality} change={quality => update({ quality })} />}
        </>}
  </>
}
