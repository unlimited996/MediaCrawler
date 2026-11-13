import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Film, Loader2 } from 'lucide-react'
import { videoApi } from '@/lib/api'
import { formatBytes } from '@/lib/format'

/**
 * 视频帖详情页：展示 S3 videos 桶里的可播放视频 + 原始文件大小。
 *
 * 大小直接取后端 original_size_bytes（源片真实字节数，转码前），
 * 与 /library/api/videos 返回完全一致；hover 可看精确字节数。
 * 转储未完成时轮询，完成后自动停。
 */
export function VideoAttachment({ storyId }: { storyId: number }) {
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

  const v = q.data
  if (q.isLoading || !v) return null

  if (v.status !== 'uploaded' || !v.object_url) {
    return (
      <div className="mb-3 flex items-center gap-2 rounded-lg border border-cyber-border-subtle bg-cyber-bg-panel px-3 py-2 text-xs font-mono text-cyber-text-secondary">
        {v.status === 'failed'
          ? <AlertTriangle className="h-4 w-4 text-rose-400" />
          : <Loader2 className="h-4 w-4 animate-spin text-cyber-neon-cyan" />}
        {v.status === 'failed'
          ? `视频转储失败：${v.last_error || '未知错误'}`
          : '视频转储中（下载 / 转码 / 上传 S3），完成后自动显示播放器…'}
      </div>
    )
  }

  return (
    <div className="mb-3 overflow-hidden rounded-lg border border-cyber-border-subtle bg-cyber-bg-panel">
      <video
        controls
        playsInline
        preload="metadata"
        src={v.object_url}
        className="max-h-[480px] w-full bg-black"
      />
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-[11px] font-mono text-cyber-text-secondary">
        <span className="flex items-center gap-1 text-cyber-text-primary">
          <Film className="h-3.5 w-3.5" /> 视频
        </span>
        <span
          className="font-semibold text-cyber-neon-cyan"
          title={`${v.original_size_bytes ?? 0} 字节（后端 original_size_bytes）`}
        >
          原始大小 {formatBytes(v.original_size_bytes)}
        </span>
        <span title={`${v.stored_size_bytes ?? 0} 字节（后端 stored_size_bytes）`}>
          转储 {formatBytes(v.stored_size_bytes)}
        </span>
        <span className="text-cyber-text-muted">
          {v.codec_v || 'h264'} / {v.codec_a || 'aac'} · MP4
        </span>
        {v.duration_sec ? (
          <span className="text-cyber-text-muted">{v.duration_sec.toFixed(1)}s</span>
        ) : null}
      </div>
    </div>
  )
}
