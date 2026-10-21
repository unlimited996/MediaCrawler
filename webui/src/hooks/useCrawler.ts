import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { crawlerApi, configApi, crawlerQueueApi } from '@/lib/api'
import type { QueueAddPayload, QueueList } from '@/types/crawler'
import { useCrawlerStore } from '@/store/crawlerStore'
import type { CrawlerConfig } from '@/types/crawler'

export function useCrawlerStatus() {
  const setStatus = useCrawlerStore((state) => state.setStatus)
  const setRunningInfo = useCrawlerStore((state) => state.setRunningInfo)

  return useQuery({
    queryKey: ['crawlerStatus'],
    queryFn: async () => {
      const { data } = await crawlerApi.getStatus()
      setStatus(data.status)
      setRunningInfo(data.platform, data.crawler_type, data.started_at)
      return data
    },
    refetchInterval: 2000,
  })
}

export function useStartCrawler() {
  const queryClient = useQueryClient()
  const setStatus = useCrawlerStore((state) => state.setStatus)
  const clearLogs = useCrawlerStore((state) => state.clearLogs)

  return useMutation({
    mutationFn: (config: CrawlerConfig) => crawlerApi.start(config),
    onMutate: () => {
      clearLogs()
      setStatus('running')
    },
    onSuccess: (res) => {
      // 爬虫忙时后端会把这次请求排进队列，而不是直接拒绝
      const queued = (res.data as { queued?: number } | undefined)?.queued
      if (queued) {
        toast.info(`爬虫正在运行，已加入队列 ${queued} 条，前一个跑完会自动执行`)
      } else {
        toast.success('Crawler started successfully')
      }
      queryClient.invalidateQueries({ queryKey: ['crawlerStatus'] })
      queryClient.invalidateQueries({ queryKey: ['crawlerQueue'] })
    },
    onError: (error: Error) => {
      setStatus('idle')
      toast.error(`Failed to start crawler: ${error.message}`)
    },
  })
}

export function useStopCrawler() {
  const queryClient = useQueryClient()
  const setStatus = useCrawlerStore((state) => state.setStatus)

  return useMutation({
    mutationFn: () => crawlerApi.stop(),
    onMutate: () => {
      setStatus('stopping')
    },
    onSuccess: () => {
      toast.success('Crawler stopped')
      setStatus('idle')
      queryClient.invalidateQueries({ queryKey: ['crawlerStatus'] })
      // 终止会把进行中的队列任务标成 cancelled，并暂停队列续跑
      queryClient.invalidateQueries({ queryKey: ['crawlerQueue'] })
    },
    onError: (error: Error) => {
      toast.error(`Failed to stop crawler: ${error.message}`)
    },
  })
}

// ---------------- 抓取队列 ----------------

const QK = ['crawlerQueue']

/** 队列列表：2s 轮询，任务状态变化能及时反映到面板上 */
export function useCrawlerQueue(refetchInterval: number | false = 2000) {
  return useQuery({
    queryKey: QK,
    queryFn: async () => (await crawlerQueueApi.list()).data as QueueList,
    refetchInterval,
  })
}

function useQueueInvalidate() {
  const queryClient = useQueryClient()
  return () => {
    queryClient.invalidateQueries({ queryKey: QK })
    queryClient.invalidateQueries({ queryKey: ['crawlerStatus'] })
  }
}

export function useQueueAdd() {
  const invalidate = useQueueInvalidate()
  return useMutation({
    mutationFn: (payload: QueueAddPayload) => crawlerQueueApi.add(payload),
    onSuccess: (res) => {
      const { added = [], duplicates = [] } = res.data
      if (added.length) toast.success(`已加入队列 ${added.length} 条`)
      if (duplicates.length) toast.warning(`跳过重复 ${duplicates.length} 条`)
      invalidate()
    },
    onError: (error: Error) => toast.error(`加入队列失败: ${error.message}`),
  })
}

export function useQueueRemove() {
  const invalidate = useQueueInvalidate()
  return useMutation({
    mutationFn: (id: number) => crawlerQueueApi.remove(id),
    onSuccess: () => {
      toast.success('已从队列移除')
      invalidate()
    },
    onError: (error: Error) => toast.error(`移除失败: ${error.message}`),
  })
}

export function useQueueClear() {
  const invalidate = useQueueInvalidate()
  return useMutation({
    mutationFn: (statuses?: string[]) => crawlerQueueApi.clear(statuses),
    onSuccess: (res) => {
      toast.success(`已清空 ${res.data.removed} 条`)
      invalidate()
    },
    onError: (error: Error) => toast.error(`清空失败: ${error.message}`),
  })
}

export function useQueueRetry() {
  const invalidate = useQueueInvalidate()
  return useMutation({
    mutationFn: (payload: { ids?: number[]; all?: boolean }) => crawlerQueueApi.retry(payload),
    onSuccess: (res) => {
      toast.success(`已重新排队 ${res.data.requeued} 条`)
      invalidate()
    },
    onError: (error: Error) => toast.error(`重试失败: ${error.message}`),
  })
}

export function useQueueResume() {
  const invalidate = useQueueInvalidate()
  return useMutation({
    mutationFn: () => crawlerQueueApi.resume(),
    onSuccess: (res) => {
      toast.success(res.data.message || '队列已恢复')
      invalidate()
    },
    onError: (error: Error) => toast.error(`恢复失败: ${error.message}`),
  })
}

export function useCrawlerLogs() {
  const setLogs = useCrawlerStore((state) => state.setLogs)

  return useQuery({
    queryKey: ['crawlerLogs'],
    queryFn: async () => {
      const { data } = await crawlerApi.getLogs(500)
      setLogs(data.logs)
      return data.logs
    },
    refetchInterval: false, // Use WebSocket instead
  })
}

export function usePlatforms() {
  return useQuery({
    queryKey: ['platforms'],
    queryFn: async () => {
      const { data } = await configApi.getPlatforms()
      return data.platforms
    },
    staleTime: Infinity,
  })
}

export function useConfigOptions() {
  return useQuery({
    queryKey: ['configOptions'],
    queryFn: async () => {
      const { data } = await configApi.getOptions()
      return data
    },
    staleTime: Infinity,
  })
}
