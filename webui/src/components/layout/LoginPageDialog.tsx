import { useCallback, useEffect, useRef, useState } from 'react'
import { Monitor, X, Loader2, CheckCircle2, ChevronUp, ChevronDown } from 'lucide-react'
import { toast } from 'sonner'
import { loginSessionApi, type LoginSessionStatus } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

/**
 * WebUI 里的「原版登录页」：容器内 headless 浏览器打开小红书原版页面，
 * 截图推到这里，点击坐标 / 文本输入回传给 Playwright 执行。
 * 登录态由 Chromium 直接写进共享的 browser_data 卷，无需导出 Cookie。
 */
export function LoginPageDialog() {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [viewport, setViewport] = useState({ width: 1280, height: 800 })
  const [status, setStatus] = useState<LoginSessionStatus | null>(null)
  const [text, setText] = useState('')
  const [tick, setTick] = useState(0)
  const imgRef = useRef<HTMLImageElement>(null)

  // 截图刷新：加时间戳绕开浏览器缓存
  useEffect(() => {
    if (!open) return
    const t = setInterval(() => setTick((v) => v + 1), 800)
    return () => clearInterval(t)
  }, [open])

  // 登录态 / 会话状态轮询
  useEffect(() => {
    if (!open) return
    let alive = true
    const poll = async () => {
      try {
        const { data } = await loginSessionApi.status()
        if (alive) setStatus(data)
      } catch { /* 会话被后端超时关闭时忽略 */ }
    }
    void poll()
    const t = setInterval(poll, 2000)
    return () => { alive = false; clearInterval(t) }
  }, [open])

  const start = useCallback(async () => {
    setBusy(true)
    try {
      const { data } = await loginSessionApi.start()
      setViewport(data.viewport || { width: 1280, height: 800 })
      setStatus(null)
      setOpen(true)
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || '打开登录页失败')
    } finally {
      setBusy(false)
    }
  }, [])

  const stop = useCallback(async () => {
    setBusy(true)
    try {
      const { data } = await loginSessionApi.stop()
      toast[data.logged_in ? 'success' : 'info'](data.message)
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || '关闭会话失败')
    } finally {
      setOpen(false)
      setStatus(null)
      setBusy(false)
    }
  }, [])

  const send = useCallback(async (payload: {
    action: string; x?: number; y?: number; text?: string; key?: string; dy?: number
  }) => {
    try {
      await loginSessionApi.input(payload)
      setTick((v) => v + 1)
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || '操作失败')
    }
  }, [])

  const onImageClick = useCallback((e: React.MouseEvent<HTMLImageElement>) => {
    const el = imgRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const x = ((e.clientX - rect.left) / rect.width) * viewport.width
    const y = ((e.clientY - rect.top) / rect.height) * viewport.height
    void send({ action: 'click', x, y })
  }, [send, viewport])

  const submitText = useCallback(() => {
    if (!text) return
    void send({ action: 'type', text })
    setText('')
  }, [text, send])

  if (!open) {
    return (
      <button
        onClick={start}
        disabled={busy}
        title="打开原版登录页（扫码 / 手机号短信验证码）"
        className="fixed bottom-5 right-[5.5rem] z-50 flex h-11 items-center gap-2 rounded-lg border border-cyber-border-DEFAULT bg-cyber-bg-panel px-3 text-xs font-mono text-cyber-text-primary shadow-lg hover:border-cyber-neon-cyan/60 disabled:opacity-50"
      >
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Monitor className="h-4 w-4" />}
        原版登录页
      </button>
    )
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 p-4">
      <div className="flex max-h-[92vh] w-full max-w-4xl flex-col overflow-hidden rounded-lg border border-cyber-border-DEFAULT bg-cyber-bg-panel shadow-2xl">
        {/* 标题栏 */}
        <div className="flex items-center gap-2 border-b border-cyber-border-subtle px-4 py-2.5">
          <Monitor className="h-4 w-4 text-cyber-neon-cyan-dim" />
          <span className="text-sm font-mono font-semibold text-cyber-text-primary">原版登录页</span>
          {status?.logged_in && (
            <span className="flex items-center gap-1 text-xs font-mono text-cyber-neon-green-dim">
              <CheckCircle2 className="h-3.5 w-3.5" /> 已登录
            </span>
          )}
          <span className="ml-auto text-[11px] font-mono text-cyber-text-muted">
            {status?.message || '正在打开…'}
          </span>
          <Button variant="ghost" size="sm" className="h-7 px-2" onClick={stop} title="关闭">
            <X className="h-4 w-4" />
          </Button>
        </div>

        {/* 原版页面截图：点哪里就点哪里 */}
        <div className="flex-1 overflow-auto bg-[#0d1117] p-2">
          <img
            ref={imgRef}
            src={`/crawler/api/crawler/login-session/screenshot?t=${tick}`}
            alt="小红书登录页"
            onClick={onImageClick}
            className="mx-auto block w-full max-w-[1280px] cursor-crosshair select-none"
            draggable={false}
          />
        </div>

        {/* 输入工具条 */}
        <div className="space-y-2 border-t border-cyber-border-subtle px-4 py-3">
          <p className="text-[11px] font-mono text-cyber-text-muted">
            先点上面的输入框获得焦点，再在下面打字。二维码、手机号、短信验证码都在原版页面里操作。
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') submitText() }}
              placeholder="手机号 / 验证码，回车发送"
              className="h-8 w-64 font-mono text-xs"
            />
            <Button size="sm" className="h-8 font-mono text-xs" onClick={submitText} disabled={!text}>
              输入
            </Button>
            <Button size="sm" variant="outline" className="h-8 font-mono text-xs"
              onClick={() => void send({ action: 'key', key: 'Enter' })}>回车</Button>
            <Button size="sm" variant="outline" className="h-8 font-mono text-xs"
              onClick={() => void send({ action: 'key', key: 'Backspace' })}>退格</Button>
            <Button size="sm" variant="outline" className="h-8 font-mono text-xs"
              onClick={() => void send({ action: 'scroll', dy: -400 })}>
              <ChevronUp className="h-3.5 w-3.5" />
            </Button>
            <Button size="sm" variant="outline" className="h-8 font-mono text-xs"
              onClick={() => void send({ action: 'scroll', dy: 400 })}>
              <ChevronDown className="h-3.5 w-3.5" />
            </Button>
          </div>
          <div className="flex items-center gap-2 pt-1">
            <span className="text-[10px] font-mono text-cyber-text-muted">
              会话剩余 {Math.round((status?.idle_left_sec || 0) / 60)} 分钟；登录态会自动写进容器卷
            </span>
            <Button size="sm" className="ml-auto h-8 font-mono text-xs" onClick={stop} disabled={busy}>
              完成登录并保存
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
