import { useCallback, useEffect, useRef, useState } from 'react'
import { ShieldAlert, WifiOff, X, Loader2, ChevronUp, ChevronDown } from 'lucide-react'
import { toast } from 'sonner'
import { captchaSessionApi, type CaptchaSessionStatus } from '@/lib/api'
import { useCrawlerStore } from '@/store/crawlerStore'
import type { LogEntry } from '@/types/crawler'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

/**
 * 抓取任务命中安全验证时自动弹出的「远程验证窗口」。
 *
 * 容器里没有图形界面，抓取进程原本想弹的「可见浏览器窗口」根本不存在。
 * 现在抓取进程把它那个停在验证页的 headless 页面截图推到共享目录，
 * 这里显示并回传点击 / 滑块拖拽 / 文本输入；完成后自动回灌 Cookie 继续抓取。
 *
 * 手机端默认显示 1920x1080 原始尺寸并可拖动，避免验证码被压得看不清。
 */
export function CaptchaDialog() {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<CaptchaSessionStatus | null>(null)
  const [text, setText] = useState('')
  const [tick, setTick] = useState(0)
  const [imgError, setImgError] = useState(false)
  /** 连续轮询失败次数：>=2 就显示可见的警告条，不再静默吞掉 404/502 */
  const [pollError, setPollError] = useState<string | null>(null)
  const [fit, setFit] = useState(
    typeof window !== 'undefined' ? window.innerWidth >= 768 : true)
  const imgRef = useRef<HTMLImageElement>(null)
  const prevActive = useRef(false)
  const dragStart = useRef<{ x: number; y: number } | null>(null)
  const failCount = useRef(0)
  const handledLogs = useRef(new Set<string>())
  // 抓取主浏览器 launch_browser() 用的视口，坐标按它换算
  const viewport = { width: 1920, height: 1080 }

  const describeError = (e: any) => {
    const r = e?.response
    if (r) return `HTTP ${r.status}${r.data?.detail ? ` · ${r.data.detail}` : ''}`
    return e?.message || '网络不可达'
  }

  // 无论弹窗是否打开都轮询：active 由 false -> true 时自动弹出。
  // 轮询接口异常必须可见：以前 catch 里什么都不做，接口 404/502 时用户只会
  // 看到「日志说已推送验证页，页面上却没有弹窗」，完全无从下手。
  const poll = useCallback(async () => {
    try {
      const { data } = await captchaSessionApi.status()
      failCount.current = 0
      setPollError(null)
      setStatus(data)
      if (data.active && !prevActive.current) {
        setOpen(true)
        setImgError(false)
        setTick((v) => v + 1)
        toast.warning('抓取任务命中安全验证，请完成验证')
      }
      if (!data.active && prevActive.current) setOpen(false)
      prevActive.current = data.active
    } catch (e: any) {
      failCount.current += 1
      if (failCount.current >= 2) setPollError(describeError(e))
    }
  }, [])

  useEffect(() => {
    void poll()
    const t = setInterval(poll, 1000)
    return () => clearInterval(t)
  }, [poll])

  // 兜底：实时日志 WS 出现「验证页已推送」就立刻开窗。
  // 用户能看到实时日志说明 WS 是通的，即使 HTTP status 接口被网关/旧镜像
  // 挡掉，也不会再出现「什么都没有」的静默失败。
  useEffect(() => {
    const isFresh = (ts: string) => {
      if (!/^\d{2}:\d{2}:\d{2}$/.test(ts)) return false
      const [h, m, s] = ts.split(':').map(Number)
      const now = new Date()
      const cur = now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds()
      const target = h * 3600 + m * 60 + s
      const diff = Math.abs(cur - target)
      return Math.min(diff, 86400 - diff) <= 90
    }
    const onLogs = (logs: LogEntry[]) => {
      for (const l of logs.slice(-8)) {
        if (!l.timestamp || !isFresh(l.timestamp)) continue
        const hit = (l.message.includes('[captcha-bridge]')
                     && l.message.includes('已推送验证会话'))
                 || l.message.includes('触发小红书安全验证')
        if (!hit) continue
        const key = `${l.timestamp}|${l.message}`
        if (handledLogs.current.has(key)) continue
        handledLogs.current.add(key)
        if (handledLogs.current.size > 40) {
          handledLogs.current = new Set([...handledLogs.current].slice(-20))
        }
        setOpen(true)
        setImgError(false)
        setTick((v) => v + 1)
        toast.warning('抓取任务命中安全验证，正在打开验证窗口')
        void poll()
      }
    }
    // 用 store.subscribe 而不是 selector，避免高频日志让整个弹窗反复重渲染
    return useCrawlerStore.subscribe((state, prev) => {
      if (state.logs !== prev.logs) onLogs(state.logs)
    })
  }, [poll])

  // 截图刷新：加时间戳绕开浏览器缓存
  useEffect(() => {
    if (!open) return
    const t = setInterval(() => setTick((v) => v + 1), 800)
    return () => clearInterval(t)
  }, [open])

  const send = useCallback(async (payload: {
    action: string; x?: number; y?: number; x2?: number; y2?: number
    text?: string; key?: string; dy?: number
  }) => {
    try {
      await captchaSessionApi.input(payload)
      setTick((v) => v + 1)
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || '操作失败')
    }
  }, [])

  const toPage = useCallback((clientX: number, clientY: number) => {
    const el = imgRef.current
    if (!el) return null
    const rect = el.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return null
    return {
      x: ((clientX - rect.left) / rect.width) * viewport.width,
      y: ((clientY - rect.top) / rect.height) * viewport.height,
    }
  }, [])

  // 轻点 = click；按住拖动 = drag（滑块验证）
  const onPointerDown = useCallback((e: React.PointerEvent<HTMLImageElement>) => {
    dragStart.current = toPage(e.clientX, e.clientY)
  }, [toPage])

  const onPointerUp = useCallback((e: React.PointerEvent<HTMLImageElement>) => {
    const start = dragStart.current
    dragStart.current = null
    const end = toPage(e.clientX, e.clientY)
    if (!start || !end) return
    if (Math.hypot(end.x - start.x, end.y - start.y) < 8) {
      void send({ action: 'click', x: start.x, y: start.y })
    } else {
      void send({ action: 'drag', x: start.x, y: start.y, x2: end.x, y2: end.y })
    }
  }, [toPage, send])

  const submitText = useCallback(() => {
    if (!text) return
    void send({ action: 'type', text })
    setText('')
  }, [text, send])

  const finish = useCallback(async () => {
    setBusy(true)
    try {
      const { data } = await captchaSessionApi.done()
      toast.success(data.message || '已通知抓取任务继续')
      setOpen(false)
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || '通知失败')
    } finally {
      setBusy(false)
    }
  }, [])

  // 会话还在但用户关了弹窗：留一个可重新打开的入口；
  // 接口连不上时也要有可见提示（点击可立即重试并手动打开验证窗口）。
  if (!open) {
    return (
      <>
        {status?.active && (
          <button
            onClick={() => setOpen(true)}
            className="fixed bottom-20 right-3 z-[200] flex h-11 items-center gap-2 rounded-lg border border-cyber-neon-orange bg-cyber-bg-panel px-3 text-xs font-mono text-cyber-neon-orange shadow-lg animate-pulse sm:bottom-5 sm:right-5"
          >
            <ShieldAlert className="h-4 w-4" /> 安全验证待处理
          </button>
        )}
        {pollError && (
          <button
            onClick={() => {
              setPollError(null)
              failCount.current = 0
              void poll()
            }}
            title="点击重试；若仍失败，检查 crawler 服务的 /crawler/captcha-session/status 是否可达"
            className="fixed bottom-36 right-3 z-[200] flex max-w-[22rem] items-center gap-2 rounded-lg border border-rose-400 bg-cyber-bg-panel px-3 py-2 text-left text-[11px] font-mono text-rose-400 shadow-lg sm:bottom-20 sm:right-5"
          >
            <WifiOff className="h-4 w-4 shrink-0" />
            <span>安全验证接口不可用：{pollError}<br />点击重试</span>
          </button>
        )}
      </>
    )
  }

  return (
    <div className="fixed inset-0 z-[200] flex items-center justify-center bg-black/80 p-0 sm:p-4">
      <div className="flex h-full w-full max-w-none flex-col overflow-hidden rounded-none border-2 border-cyber-neon-orange bg-cyber-bg-panel shadow-2xl sm:h-auto sm:max-h-[94vh] sm:max-w-5xl sm:rounded-lg">
        {/* 标题栏 */}
        <div className="flex items-center gap-2 border-b border-cyber-border-subtle px-3 py-2 sm:px-4 sm:py-2.5">
          <ShieldAlert className="h-4 w-4 shrink-0 text-cyber-neon-orange" />
          <span className="text-sm font-mono font-semibold text-cyber-text-primary">安全验证</span>
          <span className="hidden text-[11px] font-mono text-cyber-text-muted sm:inline">
            {status?.message || '请在下方完成验证'}
          </span>
          <Button variant="ghost" size="sm" className="ml-auto h-7 px-2"
            onClick={() => setOpen(false)} title="最小化（任务仍在等待）">
            <X className="h-4 w-4" />
          </Button>
        </div>

        {/* 验证页截图：轻点=点击，按住拖动=滑块；加载失败要给出可见原因，
            否则用户会以为「弹窗有了但没有滑块」 */}
        <div className="relative min-h-0 flex-1 overflow-auto overscroll-contain bg-[#0d1117] p-1 sm:p-2">
          <img
            ref={imgRef}
            src={`/crawler/api/crawler/captcha-session/screenshot?t=${tick}`}
            alt="安全验证页面"
            onPointerDown={onPointerDown}
            onPointerUp={onPointerUp}
            onLoad={() => setImgError(false)}
            onError={() => setImgError(true)}
            className={`mx-auto block cursor-crosshair select-none touch-none ${
              fit ? 'w-full max-w-[1280px]' : 'w-[1920px] max-w-none'}`}
            draggable={false}
          />
          {imgError && (
            <div className="absolute inset-0 flex items-center justify-center bg-[#0d1117]/92 p-4">
              <div className="space-y-2 text-center text-[11px] font-mono text-cyber-text-secondary">
                <p className="text-cyber-neon-orange">验证页截图加载失败（接口没返回图片）</p>
                <p>可能原因：crawler 服务不可达 / 会话已结束 / 截图尚未生成。</p>
                <p className="text-cyber-text-muted">
                  status={status?.reason || '未知'} ·
                  active={String(status?.debug?.active_exists ?? '?')} ·
                  frame={String(status?.debug?.frame_exists ?? '?')}
                </p>
                <Button size="sm" variant="outline" className="h-7 font-mono text-xs"
                  onClick={() => { setImgError(false); setTick((v) => v + 1) }}>
                  重试加载截图
                </Button>
              </div>
            </div>
          )}
        </div>

        {/* 工具条 */}
        <div className="space-y-2 border-t border-cyber-border-subtle bg-cyber-bg-panel px-3 py-2 sm:px-4 sm:py-3">
          <p className="text-[11px] font-mono text-cyber-text-muted">
            轻点=点击，按住拖动=滑块验证；先点页面里的输入框再用下方打字。手机上可左右拖动查看完整页面。
          </p>
          {status?.debug?.session_dir && (
            <p className="text-[10px] font-mono text-cyber-text-muted/70">
              文件桥：{status.debug.session_dir} · active={String(status.debug.active_exists)} ·
              done={String(status.debug.done_exists)} · frame={status.debug.frame_bytes}B
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Input
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') submitText() }}
              placeholder="手机号 / 验证码，回车发送"
              className="h-8 w-full font-mono text-xs sm:w-56"
            />
            <Button size="sm" className="h-8 font-mono text-xs" onClick={submitText} disabled={!text}>输入</Button>
            <Button size="sm" variant="outline" className="h-8 font-mono text-xs"
              onClick={() => void send({ action: 'key', key: 'Enter' })}>回车</Button>
            <Button size="sm" variant="outline" className="h-8 font-mono text-xs"
              onClick={() => void send({ action: 'key', key: 'Backspace' })}>退格</Button>
            <Button size="sm" variant="outline" className="h-8 font-mono text-xs"
              onClick={() => setFit((v) => !v)}>{fit ? '原始尺寸' : '适应屏幕'}</Button>
            <Button size="sm" variant="outline" className="h-8 font-mono text-xs"
              onClick={() => void send({ action: 'reload' })}>刷新页面</Button>
            <Button size="sm" variant="outline" className="h-8 font-mono text-xs"
              onClick={() => void send({ action: 'scroll', dy: -3000 })}>回顶部</Button>
            <Button size="sm" variant="outline" className="h-8 font-mono text-xs"
              onClick={() => void send({ action: 'scroll', dy: -400 })}><ChevronUp className="h-3.5 w-3.5" /></Button>
            <Button size="sm" variant="outline" className="h-8 font-mono text-xs"
              onClick={() => void send({ action: 'scroll', dy: 400 })}><ChevronDown className="h-3.5 w-3.5" /></Button>
          </div>
          <div className="flex items-center gap-2 pt-1">
            <span className="text-[10px] font-mono text-cyber-text-muted">
              已等待 {Math.round((status?.age_sec || 0) / 60)} 分钟；完成后抓取任务会自动继续
            </span>
            <Button size="sm" className="ml-auto h-8 font-mono text-xs" onClick={finish} disabled={busy}>
              {busy ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
              我已完成验证
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
