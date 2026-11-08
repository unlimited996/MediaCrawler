import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { QrCode, X, Loader2 } from 'lucide-react'
import { crawlerApi } from '@/lib/api'

/**
 * 扫码登录二维码（浮动可收纳）。
 *
 * 后台服务里用户看不到浏览器窗口和 PIL 弹窗，爬虫把二维码 base64 通过日志
 * 推给 API，这里轮询展示。设计成全局浮动小按钮：
 * - 有待扫的码时才出现，避免常驻占地方
 * - 默认收成右下角小方框（带脉冲提示），点开展开成面板，再点/X 收起
 * - 挂在 App 层，任何页面都能看到
 */
export function LoginQrcodeFab() {
  const { t } = useTranslation('config')
  const [open, setOpen] = useState(true)

  const q = useQuery({
    queryKey: ['crawlerQrcode'],
    queryFn: async () => (await crawlerApi.getQrcode()).data,
    refetchInterval: (query) => (query.state.data?.qr ? 3000 : 2000),
    staleTime: 0,
  })

  const qr = q.data?.qr
  if (!qr) return null

  const src = qr.startsWith('data:') ? qr : `data:image/png;base64,${qr}`

  return (
    <div className="fixed left-2 top-2 z-[70] flex flex-col items-start gap-2 sm:bottom-5 sm:left-auto sm:right-5 sm:top-auto sm:items-end">
      {open && (
        <div className="w-52 rounded-lg glass-panel float-panel border border-cyber-border-subtle p-2 shadow-lg animate-slide-up sm:w-64 sm:p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-[11px] font-mono font-semibold text-cyber-text-primary">
              <QrCode className="h-3.5 w-3.5 text-cyber-neon-cyan" />
              {t('queue.qrcodeTitle', '请使用小红书 App 扫描二维码')}
            </span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-cyber-text-muted transition-colors hover:text-cyber-text-primary"
              title={t('queue.qrcodeCollapse', '收起')}
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          <div className="flex justify-center rounded-md border border-cyber-neon-cyan/40 bg-white p-2">
            <img src={src} alt={t('queue.qrcodeAlt', '登录二维码')} className="h-28 w-28 sm:h-40 sm:w-40" />
          </div>
          <p className="mt-2 text-[10px] leading-4 text-cyber-text-muted">
            {t('queue.qrcodeHint', '扫码并确认后会自动继续抓取。二维码 5 分钟内有效，过期请重新启动任务。')}
          </p>
          <p className="mt-1 flex items-center justify-center gap-1 text-[10px] font-mono text-cyber-neon-cyan">
            {q.isFetching ? <Loader2 className="h-2.5 w-2.5 animate-spin" /> : null}
            {t('queue.qrcodeWaiting', '等待扫码')}
          </p>
        </div>
      )}

      {/* 收起态：小方框按钮 */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={open ? t('queue.qrcodeCollapse', '收起') : t('queue.qrcodeExpand', '展开登录二维码')}
        className={`relative flex h-11 w-11 items-center justify-center rounded-lg border transition-all ${
          open
            ? 'border-cyber-neon-cyan/60 bg-cyber-neon-cyan/15 text-cyber-neon-cyan'
            : 'border-cyber-neon-cyan/40 bg-cyber-bg-secondary/90 text-cyber-neon-cyan hover:border-cyber-neon-cyan hover:bg-cyber-neon-cyan/10'
        }`}
      >
        {!open && (
          <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 animate-ping rounded-full bg-cyber-neon-green" />
        )}
        {!open && (
          <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full bg-cyber-neon-green" />
        )}
        <QrCode className="h-5 w-5" />
      </button>
    </div>
  )
}
