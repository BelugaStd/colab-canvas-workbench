import type { CardData } from './types'
import GlassSelect from './GlassSelect'
import { videoModel, videoUpscalers } from './upscale'

function Segments({ label, values, value, change }: { label: string; values: string[]; value?: string; change: (v: string) => void }) {
  return <div className="cc-setting"><span>{label}</span><div className="cc-segments">{values.map(v => <button type="button" className={value === v ? 'active' : ''} key={v} onClick={() => change(v)}>{v}</button>)}</div></div>
}

export default function CreationSettings({data,update,videoUpscale=false}:{data:CardData;update:(v:Partial<CardData>)=>void;videoUpscale?:boolean}){
  const video=data.kind==='video'
  return <>
        {video && <Segments label="参考模式" values={['首尾帧', '首帧', '首尾循环']} value={data.mode} change={mode => update({ mode })} />}
        {data.kind === 'image' && <Segments label="模型" values={['MiniMax Image', 'MOSS Image']} value={data.model === 'moss-image' ? 'MOSS Image' : 'MiniMax Image'} change={v => update({ model: v === 'MOSS Image' ? 'moss-image' : 'minimax-image' })} />}
        {data.kind === 'upscale' ? <>
          {videoUpscale && <div className="cc-setting cc-upscale-model"><span>视频超分模型</span><GlassSelect label="视频超分模型" value={videoModel(data.model).value} options={videoUpscalers} change={model=>update({model})}/><p>{videoModel(data.model).hint}</p></div>}
          <Segments label="放大倍数" values={['2×', '4×']} value={['2×','4×'].includes(data.resolution??'')?data.resolution:'2×'} change={resolution => update({ resolution })} />
          <p className="cc-setting-hint">{videoUpscale?'最高 4K · 保持画幅、帧率和声音 · 自动分段处理':'连接图片或视频，自动选择对应超分流程'}</p>
        </> : <>
          <Segments label="宽高比" values={['16:9', '3:2', '4:3', '1:1', '3:4', '9:16']} value={data.aspect} change={aspect => update({ aspect })} />
          <Segments label="分辨率" values={video ? ['512p', '768p'] : ['1K', '2K']} value={data.resolution} change={resolution => update({ resolution })} />
          {video ? <><div className="cc-setting"><span>时长 <b>{data.duration}s</b></span><input aria-label="时长" type="range" min={5} max={15} step={1} value={data.duration} onChange={e => update({ duration: +e.target.value })} /><div className="cc-range-labels"><span>5s</span><span>15s</span></div></div><Segments label="帧率" values={['24', '30']} value={String(data.fps)} change={v => update({ fps: +v })} /><div className="cc-setting cc-switch-row"><span>声音</span><button role="switch" aria-label="声音" aria-checked={!!data.audio} className="cc-switch" onClick={() => update({ audio: !data.audio })}><i /></button></div></> : <Segments label="质量" values={['标准', '精细']} value={data.quality} change={quality => update({ quality })} />}
        </>}
  </>
}
