import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, DownloadCloud, Film, Loader2, Play, X } from 'lucide-react'
import { videoApi } from '@/lib/api'
import { formatBytes } from '@/lib/format'

/**
 * 视频帖封面：图片保持原来的位置与尺寸，叠一层半透明遮罩 + ▶️ 播放标识。
 * 点击后原地切换为播放器（autoPlay），不再在页面别处单独塞一个 <video>。
 *
 * 大小取后端 original_size_bytes（转码前源片真实字节数）。
 * 存量视频帖没有转储记录时，封面上直接给「转储视频」入口，不必重抓帖子。
 * 转储中 / 失败状态也显示在封面上，避免布局跳动。
 */
export function VideoAttachment({
  storyId, isVideo = false, coverCandidates = [], className = '',
}: {
  storyId: number
  isVideo?: boolean
  /** 封面图候选源（本地 → 平台远程 → S3），由 MediaImage 传入 */
  coverCandidates?: string[]
  /** 沿用图片网格的尺寸类名，保证封面位置和比例与原来一致 */
  className?: string
}) {
  const qc = useQueryClient()
  const [playing, setPlaying] = useState(false)
  const [imgIdx, setImgIdx] = useState(0)
  const [backfillError, setBackfillError] = useState('')
  const [videoError, setVideoError] = useState('')

  const q = useQuery({
    queryKey: ['videoAsset', storyId],
    queryFn: async () =>
      (await videoApi.list({ story_id: storyId, page_size: 1 })).data.items[0] ?? null,
    staleTime: 10_000,
    refetchInterval: (query) => {
      const d = query.state.data
      return !d || d.status === 'pending' || d.status === 'uploading' ? 4000 : false
    },
  })

  const backfill = useMutation({
    mutationFn: async () => (await videoApi.backfill(storyId)).data,
    onSuccess: () => {
      setBackfillError('')
      qc.invalidateQueries({ queryKey: ['videoAsset', storyId] })
    },
    onError: (e: any) => {
      setBackfillError(e?.response?.data?.detail || e?.message || '转储失败')
    },
  })

  useEffect(() => {
    setPlaying(false)
    setImgIdx(0)
    setBackfillError('')
    setVideoError('')
  }, [storyId])

  const v = q.data
  if (!v && !isVideo) return null

  const cover = coverCandidates.length
    ? coverCandidates[Math.min(imgIdx, coverCandidates.length - 1)] : ''
  // 封面点击是「播放」而不是「放大看图」，去掉 zoom 光标；播放器不要 object-cover 裁切
  const mediaCls = className.replace(/cursor-zoom-in\s*/g, '')
  const videoCls = mediaCls.replace(/object-cover\s*/g, '')
  const playable = !!v && v.status === 'uploaded' && !!v.object_url

  const sizeBadge = v?.original_size_bytes ? (
    <span
      className="pointer-events-none absolute bottom-2 right-2 rounded bg-black/65 px-2 py-1 text-[11px] font-mono text-white/90 backdrop-blur-sm"
      title={`${v.original_size_bytes} 字节（源片原始大小）· 转储 ${formatBytes(v.stored_size_bytes)} · ${v.codec_v || 'h264'}/${v.codec_a || 'aac'} · ${v.duration_sec ?? 0}s`}
    >
      原始 {formatBytes(v.original_size_bytes)}
    </span>
  ) : null

  const poster = cover ? (
    <img
      src={cover}
      alt="视频封面"
      loading="lazy"
      // xhscdn 免签名镜像有防盗链，带本站 Referer 会 403
      referrerPolicy="no-referrer"
      className={mediaCls}
      onError={() =>
        setImgIdx((i) => (i + 1 < coverCandidates.length ? i + 1 : i))}
    />
  ) : (
    <div className="flex min-h-[180px] w-full items-center justify-center bg-gradient-to-br from-slate-800 to-slate-900">
      <Film className="h-10 w-10 text-white/40" />
    </div>
  )

  // 点击封面后：原地换成播放器，直连 S3（后端 stream 返回 307 预签名）
  if (playable && playing) {
    return (
      <div className="relative w-full overflow-hidden rounded-lg bg-black">
        <video
          controls
          autoPlay
          playsInline
          preload="metadata"
          src={v.object_url || undefined}
          className={`${videoCls} bg-black`}
          onError={() => setVideoError('视频加载失败，请检查对象存储是否可访问')}
        />
        <button
          type="button"
          onClick={() => { setPlaying(false); setVideoError('') }}
          title="返回封面"
          className="absolute right-2 top-2 flex h-7 w-7 items-center justify-center rounded-full bg-black/65 text-white/90 backdrop-blur-sm hover:bg-black/85"
        >
          <X className="h-4 w-4" />
        </button>
        {sizeBadge}
        {videoError && (
          <div className="absolute inset-x-2 bottom-2 rounded bg-rose-950/85 px-2 py-1 text-[11px] text-rose-200">
            {videoError}
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="relative w-full overflow-hidden rounded-lg bg-black">
      {poster}
      {playable ? (
        <button
          type="button"
          onClick={() => { setVideoError(''); setPlaying(true) }}
          title="点击播放视频"
          className="group absolute inset-0 flex items-center justify-center bg-black/25 transition-colors hover:bg-black/40"
        >
          <span className="flex h-14 w-14 items-center justify-center rounded-full bg-black/55 ring-1 ring-white/50 backdrop-blur-sm transition-transform group-hover:scale-110 group-hover:bg-black/75">
            <Play className="ml-1 h-7 w-7 fill-white text-white" />
          </span>
        </button>
      ) : (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/45 px-3 text-center">
          {q.isLoading ? (
            <>
              <Loader2 className="h-6 w-6 animate-spin text-white/80" />
              <span className="text-[11px] font-mono text-white/80">查询视频中…</span>
            </>
          ) : v?.status === 'failed' ? (
            <>
              <AlertTriangle className="h-6 w-6 text-rose-300" />
              <span className="text-[11px] font-mono text-rose-200">
                转储失败：{v.last_error || '未知错误'}
              </span>
            </>
          ) : !v ? (
            <>
              <span className="text-[11px] font-mono text-white/85">视频帖：尚未转储到 S3</span>
              <button
                type="button"
                disabled={backfill.isPending}
                onClick={() => backfill.mutate()}
                className="inline-flex items-center gap-1 rounded-md border border-white/40 bg-black/55 px-2.5 py-1 text-[11px] text-white hover:bg-black/75 disabled:opacity-50"
              >
                {backfill.isPending
                  ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  : <DownloadCloud className="h-3.5 w-3.5" />}
                转储视频
              </button>
            </>
          ) : (
            <>
              <Loader2 className="h-6 w-6 animate-spin text-white/80" />
              <span className="text-[11px] font-mono text-white/80">
                视频转储中（下载 / 转码 / 上传），完成后自动可播…
              </span>
            </>
          )}
          {backfillError && (
            <span className="text-[11px] text-rose-300">转储失败：{backfillError}</span>
          )}
        </div>
      )}
      {sizeBadge}
    </div>
  )
}
