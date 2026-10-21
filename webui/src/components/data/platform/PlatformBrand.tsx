/**
 * 平台品牌标识：logo → 内置图标 → 品牌色首字（三级降级）
 *
 * 优先级：
 *   1. public/logos 下有的平台用真实 logo 图（目前 小红书 / 抖音 / B站）
 *   2. 其余平台用内置 SVG 图标（形状贴近各平台标识，纯几何绘制，无外部依赖）
 *   3. 未知平台 / 图片加载失败 → 品牌色圆角块 + 平台名首字
 * 任何一级都不会出现破图或空白。
 */

import { useState } from 'react'
import { getPlatformTheme } from './platformTheme'

/** 本地 logo 资源（放在 public/logos 下，缺失的走图标 / 首字降级） */
const LOGO_MAP: Record<string, string> = {
  xhs: '/logos/xiaohongshu_logo.png',
  dy: '/logos/douyin.png',
  bili: '/logos/bilibili_logo.png',
}

/** 内置 SVG 图标（viewBox 0 0 24 24，currentColor 填充，颜色由品牌色驱动） */
const ICONS: Record<string, JSX.Element> = {
  // 小红书：书本（logo 图缺失时的兜底图标）
  xhs: (
    <path d="M4.5 4h6.2c1.7 0 3 1.3 3 3v13H7.5c-1.7 0-3-1.3-3-3V4zm0 2.2v10.8c0 .4.3.8.8.8h5.4V7c0-.4-.3-.8-.8-.8H4.5zm7.8 0h6.7c.4 0 .8.3.8.8v11.4c0 .4-.3.8-.8.8h-6.7V6.2zm1.8 2v2.2h3.1V8.2h-3.1zm0 3.6v2.2h3.1v-2.2h-3.1z" />
  ),
  // 知乎：Z 形字标
  zhihu: (
    <path d="M4 5h6.2v2.1H8.6c-.3 1.4-.9 2.7-1.7 3.9l1.4 1.3c1-1.4 1.8-2.9 2.3-4.5v9.7h5.6l-2.1 1.6 1.2 2.1 4.6-3.7-4.6-3.7-1.2 2.1 1.9 1.5H12V5h6v13h2V3H9.8v2H4z" />
  ),
  // 微博：眼睛 + 音符感的圆弧
  weibo: (
    <>
      <path d="M9.6 19.5C6.4 19.5 3.9 17 3.9 14c0-3 2.4-5.5 5.4-5.5 1.6 0 3 .7 4 1.8 1 .3 1.9.9 2.6 1.6 1.9-1.2 3-1 3-1 .2 1.3-.4 2.4-1.2 3.2 1 .9 1.6 2 1.6 3.3 0 .5-.1 1-.3 1.4 1.4.2 2.4.5 2.4.5s-1.6 1-3.6 1.2c-.6.3-1.3.5-2 .6-.8.1-1.6.2-2.4.2-.9 0-1.7-.1-2.5-.2-.5-.1-1-.2-1.5-.3z" />
      <circle cx="9.2" cy="13.6" r="3.4" fill="#fff" />
      <circle cx="9.2" cy="13.6" r="1.6" />
    </>
  ),
  // 抖音：音符
  douyin: (
    <path d="M14.2 2h2.6c.2 1.6 1.1 3 2.5 3.7.5.3 1 .5 1.6.6v2.7c-1.1 0-2.2-.3-3.2-.8v5.9c0 3.6-2.9 6.5-6.5 6.5S4.7 17.1 4.7 13.5s2.9-6.5 6.5-6.5c.4 0 .7 0 1.1.1v2.8c-.3-.1-.7-.1-1.1-.1-2 0-3.7 1.7-3.7 3.7s1.7 3.7 3.7 3.7 3.7-1.7 3.7-3.7V2z" />
  ),
  // B站：电视 + 天线
  bili: (
    <path d="M6.6 3.2 9 5.6h6l2.4-2.4h2.2v13.2c0 1.7-1.4 3.1-3.1 3.1H7.5c-1.7 0-3.1-1.4-3.1-3.1V3.2h2.2zM8.3 8.4v5.2h2.2V8.4H8.3zm5.2 0v5.2h2.2V8.4h-2.2z" />
  ),
  // 贴吧：对话框 + 感叹号
  tieba: (
    <>
      <path d="M3 5.5A2.5 2.5 0 0 1 5.5 3h13A2.5 2.5 0 0 1 21 5.5v9a2.5 2.5 0 0 1-2.5 2.5H9l-4.5 4v-4H5.5A2.5 2.5 0 0 1 3 14.5v-9z" />
      <path d="M12 6.6v5.2M12 14.4v.6" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" fill="none" />
    </>
  ),
  // 快手：快字感的双弧
  kuaishou: (
    <path d="M12 2.6c1.9 0 3.5.6 4.7 1.6 1.9 1.6 2.6 4.1 2.6 7.1 0 2.9-.8 5.1-2.3 6.7-1 .5-1.9.5-2.9.5h-4.2c-1 0-1.9 0-2.9-.5C5.5 16.4 4.7 14.2 4.7 11.3c0-3 .7-5.5 2.6-7.1C8.5 3.2 10.1 2.6 12 2.6zm-2.2 4.4v6.2l1.6-1.5 1.6 1.5V7h-3.2z" />
  ),
  // X / Twitter
  twitter: (
    <path d="M17.5 3h3.1l-6.8 7.8L21.8 21h-6.2l-4.9-6.4L5 21H2l7.3-8.3L2.5 3h6.4l4.4 5.8L17.5 3z" />
  ),
  // Facebook
  facebook: (
    <path d="M13.5 21v-7.5h2.6l.4-3h-3V8.6c0-.9.2-1.4 1.5-1.4h1.5V4.6c-.3 0-1.3-.1-2.4-.1-2.4 0-4.1 1.5-4.1 4.2v2.3H7.4v3h2.6V21h3.5z" />
  ),
  // Reddit
  reddit: (
    <>
      <circle cx="12" cy="13.5" r="6.2" />
      <circle cx="16.4" cy="6.6" r="2" />
      <path d="M12 10.6c1.2 0 2.2 1 2.2 2.3 0 1.2-1 2.2-2.2 2.2s-2.2-1-2.2-2.2c0-1.3 1-2.3 2.2-2.3zm-1.6.6a.6.6 0 1 0 0 1.2.6.6 0 0 0 0-1.2zm3.2 0a.6.6 0 1 0 0 1.2.6.6 0 0 0 0-1.2z" fill="#fff" />
    </>
  ),
}

/** 平台 key → 图标 key（layout 名与平台 key 不一致的平台在这里映射） */
const ICON_KEY: Record<string, string> = {
  xhs: 'xhs',
  zhihu: 'zhihu',
  wb: 'weibo',
  dy: 'douyin',
  bili: 'bili',
  tieba: 'tieba',
  ks: 'kuaishou',
  twitter: 'twitter',
  facebook: 'facebook',
  reddit: 'reddit',
}

interface BrandMarkProps {
  platform?: string | null
  size?: number
  /** 圆形底板（侧栏 / 列表用小圆底更整齐） */
  rounded?: boolean
  className?: string
}

/**
 * 平台品牌标识。
 * 有 logo 图 → 用图；否则内置 SVG 图标；再不行 → 品牌色 + 平台名首字。
 */
export function PlatformBrandMark({
  platform, size = 16, rounded = true, className = '',
}: BrandMarkProps) {
  const theme = getPlatformTheme(platform)
  const [imgFailed, setImgFailed] = useState(false)
  const logo = LOGO_MAP[platform || '']
  const icon = ICONS[ICON_KEY[platform || ''] || '']
  const box = rounded ? 'rounded-full' : 'rounded-md'
  const style = { width: size, height: size, flexShrink: 0 } as const

  if (logo && !imgFailed) {
    return (
      <img
        src={logo}
        alt={theme.label}
        title={theme.label}
        width={size}
        height={size}
        loading="lazy"
        onError={() => setImgFailed(true)}
        className={`${box} object-contain ${className}`}
        style={{ ...style, background: '#fff' }}
      />
    )
  }

  if (icon) {
    return (
      <span
        title={theme.label}
        className={`inline-flex items-center justify-center ${box} ${className}`}
        style={{ ...style, background: theme.accentSoft, color: theme.accent }}
      >
        <svg viewBox="0 0 24 24" width={size * 0.72} height={size * 0.72}
             fill="currentColor" aria-hidden="true">
          {icon}
        </svg>
      </span>
    )
  }

  // 降级：品牌色底 + 平台名首字
  return (
    <span
      title={theme.label}
      className={`inline-flex items-center justify-center font-mono font-bold ${box} ${className}`}
      style={{
        ...style,
        background: theme.accentSoft,
        color: theme.accent,
        fontSize: Math.max(9, Math.round(size * 0.55)),
      }}
    >
      {(theme.label || '?').trim().charAt(0)}
    </span>
  )
}

interface HeaderProps {
  platform?: string | null
  /** 右侧附加内容（统计等） */
  children?: React.ReactNode
}

/**
 * 平台页头部：品牌标识 + 平台名 + 品牌色描边，一眼区分当前所属平台。
 * platform 为空（全部）时用中性青色。
 */
export function PlatformPageHeader({ platform, children }: HeaderProps) {
  const theme = getPlatformTheme(platform)
  const isAll = !platform
  const accent = isAll ? '#06B6D4' : theme.accent

  return (
    <div
      className="flex flex-shrink-0 items-center gap-2.5 rounded-lg border px-3 py-2"
      style={{
        borderColor: `${accent}55`,
        background: `linear-gradient(90deg, ${isAll ? 'rgba(6,182,212,0.10)' : theme.accentSoft} 0%, rgba(0,0,0,0) 65%)`,
      }}
    >
      <PlatformBrandMark platform={platform} size={26} />
      <div className="min-w-0">
        <h2
          className="truncate text-sm font-mono font-bold leading-tight"
          style={{ color: accent }}
        >
          {isAll ? '全部平台' : theme.label}
        </h2>
        <p className="text-[10px] font-mono text-cyber-text-muted leading-tight">
          {isAll ? 'DATA · ALL PLATFORMS' : `DATA · ${platform}`}
        </p>
      </div>
      <div className="ml-auto flex flex-shrink-0 items-center gap-2">{children}</div>
    </div>
  )
}
