import { cn } from '@/lib/utils'
import type { LogEntry } from '@/types/crawler'

interface TerminalLineProps {
  log: LogEntry
}

// 控制台永远是深色底（#0d1117），所以文字色用**固定亮色**而不跟随主题：
// 浅色主题里 --cyber-neon-* 会变成中饱和的深色调（如 cyan 6,182,212），
// 落在深底上对比不足，看起来像"文字消失了"。
const levelConfig: Record<string, { text: string; bg: string; glow: string }> = {
  info: {
    text: 'text-[#79c0ff]',
    bg: 'bg-[#79c0ff]/10',
    glow: ''
  },
  success: {
    text: 'text-[#56d364]',
    bg: 'bg-[#56d364]/10',
    glow: ''
  },
  warning: {
    text: 'text-[#e3b341]',
    bg: 'bg-[#e3b341]/10',
    glow: ''
  },
  error: {
    text: 'text-[#ff7b72]',
    bg: 'bg-[#ff7b72]/10',
    glow: ''
  },
  debug: {
    text: 'text-[#8b949e]',
    bg: 'bg-[#21262d]',
    glow: ''
  },
}

const levelIcons: Record<string, string> = {
  info: 'DATA',
  success: 'OK',
  warning: 'WARN',
  error: 'ERR',
  debug: 'DBG',
}

export function TerminalLine({ log }: TerminalLineProps) {
  const config = levelConfig[log.level] || levelConfig.info

  return (
    <div className="flex gap-2 text-xs leading-relaxed font-mono group hover:bg-[#21262d]/50 px-1 -mx-1 rounded transition-colors">
      {/* Timestamp */}
      <span className="text-[#8b949e] flex-shrink-0 opacity-60 group-hover:opacity-100 transition-opacity">
        [{log.timestamp}]
      </span>

      {/* Level badge */}
      <span className={cn(
        'flex-shrink-0 w-14 px-1 rounded text-center',
        config.bg,
        config.text,
        config.glow
      )}>
        [{levelIcons[log.level]}]
      </span>

      {/* Message */}
      <span className={cn('break-all', config.text)}>
        {log.message}
      </span>
    </div>
  )
}
