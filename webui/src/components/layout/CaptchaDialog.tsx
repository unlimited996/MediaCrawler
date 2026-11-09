import { useCallback, useEffect, useRef, useState } from 'react'
import { ShieldAlert, X, Loader2, ChevronUp, ChevronDown } from 'lucide-react'
import { toast } from 'sonner'
import { captchaSessionApi, type CaptchaSessionStatus } from '@/lib/api'
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
  const [fit, setFit] = useState(
    typeof window !== 'undefined' ? window.innerWidth >= 768 : true)
  const imgRef = useRef<HTMLImageElement>(null)
  const prevActive = useRef(false)
  const dragStart = useRef<{ x: number; y: number } | null>(null)
  // 抓取主浏览器 launch_browser() 用的视口，坐标按它换算
  const viewport = { width: 1920, height: 1080 }

  // 无论弹窗是否打开都轮询：active 由 false -> true 时自动弹出
  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const { data } = await captchaSessionApi.status()
        if (!alive) return
        setStatus(data)
        if (data.active && !prevActive.current) {
          setOpen(true)
          setTick((v) => v + 1)
          toast.warning('抓取任务命中安全验证，请完成验证')
        }
        if (!data.active && prevActive.current) setOpen(false)
        prevActive.current = data.active
      } catch { /* 服务尚未启动 / 网络抖动时忽略 */ }
    }
    void poll()
    const t = setInterval(poll, 2000)
    return () => { alive = false; clearInterval(t) }
  }, [])

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

  // 会话还在但用户关了弹窗：留一个可重新打开的入口
  if (!open) {
    if (!status?.active) return null
    return (
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-20 right-3 z-[80] flex h-11 items-center gap-2 rounded-lg border border-cyber-neon-orange bg-cyber-bg-panel px-3 text-xs font-mono text-cyber-neon-orange shadow-lg animate-pulse sm:bottom-5 sm:right-5"
      >
        <ShieldAlert className="h-4 w-4" /> 安全验证待处理
      </button>
    )
  }

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/80 p-0 sm:p-4">
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

        {/* 验证页截图：轻点=点击，按住拖动=滑块 */}
        <div className="min-h-0 flex-1 overflow-auto overscroll-contain bg-[#0d1117] p-1 sm:p-2">
          <img
            ref={imgRef}
            src={`/crawler/api/crawler/captcha-session/screenshot?t=${tick}`}
            alt="安全验证页面"
            onPointerDown={onPointerDown}
            onPointerUp={onPointerUp}
            className={`mx-auto block cursor-crosshair select-none touch-none ${
              fit ? 'w-full max-w-[1280px]' : 'w-[1920px] max-w-none'}`}
            draggable={false}
          />
        </div>

        {/* 工具条 */}
        <div className="space-y-2 border-t border-cyber-border-subtle bg-cyber-bg-panel px-3 py-2 sm:px-4 sm:py-3">
          <p className="text-[11px] font-mono text-cyber-text-muted">
            轻点=点击，按住拖动=滑块验证；先点页面里的输入框再用下方打字。手机上可左右拖动查看完整页面。
          </p>
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
