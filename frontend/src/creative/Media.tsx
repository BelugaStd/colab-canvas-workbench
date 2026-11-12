import { useEffect, useRef, useState } from 'react'
import { Maximize2, Pause, Play, Volume2, VolumeX } from 'lucide-react'
import { assetUrl } from './types'

export default function Media({ id, type, label, expand, suspended }: { id: string; type?: string; label: string; expand: () => void; suspended: boolean }) {
  const video = useRef<HTMLVideoElement>(null)
  const [playing, setPlaying] = useState(false), [muted, setMuted] = useState(true)
  const [time, setTime] = useState(0), [duration, setDuration] = useState(0), [error, setError] = useState('')
  useEffect(() => { if (suspended) video.current?.pause() }, [suspended])
  async function toggle() {
    if (!video.current) return
    if (!video.current.paused) { video.current.pause(); return }
    try { await video.current.play(); setError('') } catch { setError('无法播放，请打开详情重试') }
  }
  return <div className="cc-media" onDoubleClick={expand}>
    {type?.startsWith('video') ? <>
      <video ref={video} src={assetUrl(id)} playsInline preload="auto" loop muted={muted}
        onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onTimeUpdate={e => setTime(e.currentTarget.currentTime)}
        onLoadedMetadata={e => setDuration(e.currentTarget.duration)} onError={() => setError('视频加载失败，请打开详情重试')} />
      <div className="cc-media-controls nodrag nopan" onDoubleClick={e => e.stopPropagation()}>
        <button aria-label={playing ? '暂停视频' : '播放视频'} onClick={toggle}>{playing ? <Pause size={16}/> : <Play size={16}/>}</button>
        <input aria-label="视频进度" type="range" min={0} max={duration || 1} step={.01} value={time} onChange={e => { if (video.current) video.current.currentTime = +e.target.value; setTime(+e.target.value) }}/>
        <small>{time.toFixed(1)} / {duration.toFixed(1)}s</small>
        <button aria-label={muted ? '开启视频声音' : '静音视频'} onClick={() => setMuted(!muted)}>{muted ? <VolumeX size={15}/> : <Volume2 size={15}/>}</button>
      </div>
    </> : type?.startsWith('audio') ? <audio className="nodrag nopan" controls src={assetUrl(id)}/> : <img src={assetUrl(id)} alt={label} draggable={false}/>}
    {error && <small className="cc-media-error">{error}</small>}
    <button className="cc-media-expand nodrag nopan" title="查看详情" aria-label="查看节点详情" onClick={expand}><Maximize2 size={17}/></button>
  </div>
}
