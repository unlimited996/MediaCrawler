import type { ReactNode, KeyboardEvent } from 'react'
import { useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { Image as ImageIcon, ListPlus, Loader2, MessageSquare, Play, Square, X } from 'lucide-react'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import { Button } from '@/components/ui/button'
import { useCrawlerStore } from '@/store/crawlerStore'
import { usePlatforms, useConfigOptions, useStartCrawler, useStopCrawler, useQueueAdd } from '@/hooks/useCrawler'
import { toast } from 'sonner'
import { ParsedIdList } from './ParsedIdList'
import { looksLikeUrl, detectPlatformFromUrl } from '@/lib/linkDetect'
import { parseMultipleUrls } from '@/lib/urlParser'

/** 紧凑分组：只留一条小标题 + 分隔线，不再用整块卡片（省纵向空间） */
function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <span className="text-[10px] font-mono font-bold uppercase tracking-wider text-cyber-neon-cyan/80">
          {title}
        </span>
        <span className="h-px flex-1 bg-cyber-border-subtle" />
      </div>
      <div className="space-y-2">
        {children}
      </div>
    </div>
  )
}

type FieldProps = {
  label: string
  hint?: string
  children: ReactNode
}

function Field({ label, hint, children }: FieldProps) {
  return (
    <div className="space-y-1">
      <div className="space-y-0.5">
        <Label className="text-[11px] text-cyber-text-secondary font-mono">
          {label}
        </Label>
        {hint ? (
          <p className="text-[10px] text-cyber-text-muted leading-snug">
            {hint}
          </p>
        ) : null}
      </div>
      {children}
    </div>
  )
}

type KeywordInputProps = {
  value: string
  onChange: (value: string) => void
  /** 回车触发：直接开始搜索 */
  onSubmit?: () => void
  placeholder?: string
  disabled?: boolean
}

function KeywordInput({ value, onChange, onSubmit, placeholder, disabled }: KeywordInputProps) {
  // 完全受控：输入即写回 store，不再有"输完没按回车就没生效"的坑
  const keywords = value ? value.split(',').map((k) => k.trim()).filter(Boolean) : []

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      // 回车 = 开始搜索（而不是"添加关键词"）
      onSubmit?.()
    }
  }

  const removeKeyword = (keywordToRemove: string) => {
    const newKeywords = keywords.filter((k) => k !== keywordToRemove)
    onChange(newKeywords.join(','))
  }

  return (
    <div className="space-y-2">
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        className="h-9 text-xs"
      />
      {keywords.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {keywords.map((keyword) => (
            <span
              key={keyword}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-cyber-neon-cyan/10 border border-cyber-neon-cyan/30 text-cyber-neon-cyan text-xs font-mono"
            >
              {keyword}
              {!disabled && (
                <button
                  type="button"
                  onClick={() => removeKeyword(keyword)}
                  className="hover:text-cyber-neon-pink transition-colors"
                >
                  <X className="w-3 h-3" />
                </button>
              )}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

export function CrawlerConfigPanel() {
  const { t } = useTranslation('config')
  const config = useCrawlerStore((state) => state.config)
  const updateConfig = useCrawlerStore((state) => state.updateConfig)
  const status = useCrawlerStore((state) => state.status)

  const { data: platforms } = usePlatforms()
  const { data: options } = useConfigOptions()
  const { mutate: startCrawler, isPending: isStarting } = useStartCrawler()
  const { mutate: stopCrawler, isPending: isStopping } = useStopCrawler()
  const addToQueueM = useQueueAdd()

  const isDisabled = status === 'running' || status === 'stopping'
  const isRunning = status === 'running'
  const isBusy = isStarting || isStopping || status === 'stopping'

  // 已选的存储方式不在可用列表里（数据库刚挂 / 配置被改）：给出提示
  const saveOptionMissing =
    !!options?.save_options?.length &&
    !!config.save_option &&
    !options.save_options.some((o) => o.value === config.save_option)

  // 链接识别去重：同一串输入只提示一次，避免打字过程重复弹 toast
  const lastLinkRef = useRef<string>('')

  /**
   * 关键词框粘贴链接时：
   * - 识别成功 → 自动切换平台 + 切到详情模式 + 链接移入 specified_ids
   * - 是链接但域名不认识 → 抛出错误提示
   */
  const handleKeywordsChange = (raw: string) => {
    updateConfig({ keywords: raw })
    const trimmed = raw.trim()
    if (!trimmed || !looksLikeUrl(trimmed)) {
      lastLinkRef.current = ''
      return
    }
    if (lastLinkRef.current === trimmed) return
    lastLinkRef.current = trimmed

    const hit = detectPlatformFromUrl(trimmed)
    if (hit) {
      updateConfig({
        platform: hit.platform,
        crawler_type: 'detail',
        specified_ids: trimmed,
        keywords: '',
      })
      toast.success(t('field.linkDetected', { platform: hit.label }))
    } else {
      toast.error(t('field.linkUnknown'))
    }
  }

  const handleStart = () => {
    // 兜底校验：搜索模式下没关键词就跑，后端会静默用默认关键词，
    // 用户会看到"明明填了却抓别的"的诡异现象，这里直接拦住
    if (config.crawler_type === 'search' && !config.keywords.trim()) {
      toast.error(t('field.keywordsRequired') || '请先输入搜索关键词')
      return
    }
    if (config.crawler_type === 'detail' && !config.specified_ids.trim()) {
      toast.error(t('field.specifiedIdsRequired') || '请先输入笔记/视频 ID 或链接')
      return
    }
    if (config.crawler_type === 'creator' && !config.creator_ids.trim()) {
      toast.error(t('field.creatorIdsRequired') || '请先输入创作者 ID')
      return
    }
    startCrawler(config)
  }

  const handleStop = () => {
    stopCrawler()
  }

  /**
   * 把「目标配置」里当前填的目标逐条加入队列。
   * 用 parseMultipleUrls 保留原始串（小红书链接必须带 xsec_token，丢了就抓不到）。
   */
  const currentTargets = (): string[] => {
    if (config.crawler_type === 'search') {
      return config.keywords.split(',').map((s) => s.trim()).filter(Boolean)
    }
    const raw = config.crawler_type === 'creator' ? config.creator_ids : config.specified_ids
    if (!raw.trim()) return []
    const parsed = parseMultipleUrls(raw, config.platform)
    if (parsed.length) return parsed.map((p) => p.original.trim()).filter(Boolean)
    return raw.split(/[\n,\s]+/).map((s) => s.trim()).filter(Boolean)
  }

  const handleAddToQueue = () => {
    const urls = currentTargets()
    if (!urls.length) {
      toast.error(t('queue.noTarget') || '请先在上方填写采集目标')
      return
    }
    addToQueueM.mutate({
      urls,
      platform: config.platform,
      crawler_type: config.crawler_type,
      login_type: config.login_type,
      start_page: config.start_page,
      enable_comments: config.enable_comments,
      enable_sub_comments: config.enable_sub_comments,
      enable_media: config.enable_media,
      save_option: config.save_option,
      cookies: config.cookies,
      headless: config.headless,
    })
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* 表单区：所有配置合并成一个连续表单，自身滚动 */}
      <div className="flex-1 min-h-0 overflow-y-auto px-3 pt-3 pb-2 space-y-3">
        <Group title={t('section.targetMatrix.title')}>
          <Field label={t('field.platform')}>
            <Select
              value={config.platform}
              onValueChange={(value) => updateConfig({ platform: value })}
              disabled={isDisabled}
            >
              <SelectTrigger className="h-9 text-xs">
                <SelectValue placeholder={t('field.platformPlaceholder')} />
              </SelectTrigger>
              <SelectContent>
                {platforms?.map((platform) => (
                  <SelectItem key={platform.value} value={platform.value}>
                    {platform.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label={t('field.crawlType')}>
              <Select
                value={config.crawler_type}
                onValueChange={(value) => updateConfig({ crawler_type: value })}
                disabled={isDisabled}
              >
                <SelectTrigger className="h-9 text-xs">
                  <SelectValue placeholder={t('field.crawlTypePlaceholder')} />
                </SelectTrigger>
                <SelectContent>
                  {(options?.crawler_types || []).map((type) => (
                    <SelectItem key={type.value} value={type.value}>
                      {type.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <Field label={t('field.startPage')}>
              <Input
                type="number"
                min={1}
                value={config.start_page}
                onChange={(e) => updateConfig({ start_page: parseInt(e.target.value) || 1 })}
                disabled={isDisabled}
                className="h-9 text-xs"
              />
            </Field>
          </div>

          {/* 根据爬虫类型显示不同的输入框 */}
          {config.crawler_type === 'search' && (
            <Field label={t('field.keywords')} hint={t('field.keywordsHint')}>
              <KeywordInput
                placeholder={t('field.keywordsPlaceholder')}
                value={config.keywords}
                onChange={handleKeywordsChange}
                onSubmit={handleStart}
                disabled={isDisabled}
              />
              <p className="text-[11px] font-mono text-cyber-text-muted">
                {t('field.linkHint')}
              </p>
            </Field>
          )}

          {config.crawler_type === 'detail' && (
            <Field label={t('field.specifiedIds')} hint={t('field.specifiedIdsHint')}>
              <textarea
                value={config.specified_ids}
                onChange={(e) => updateConfig({ specified_ids: e.target.value })}
                disabled={isDisabled}
                placeholder={t(`field.specifiedIdsPlaceholder.${config.platform}`, t('field.specifiedIdsPlaceholder.default'))}
                className="min-h-[60px] w-full rounded-md border border-cyber-border-DEFAULT bg-cyber-bg-tertiary px-3 py-2 text-xs font-mono text-cyber-text-primary placeholder:text-cyber-text-muted focus-visible:outline-none focus-visible:border-cyber-neon-cyan/50 focus-visible:shadow-cyber-soft disabled:cursor-not-allowed disabled:opacity-50 transition-all resize-none"
              />
              <ParsedIdList
                value={config.specified_ids}
                platform={config.platform}
                type="detail"
                disabled={isDisabled}
              />
              {config.platform === 'xhs' && (
                <div className="mt-2 rounded-lg border border-cyber-neon-orange/30 bg-cyber-neon-orange/5 p-2 text-[10px] leading-snug text-cyber-neon-orange font-mono">
                  {t('warning.xhsToken')}
                </div>
              )}
            </Field>
          )}

          {config.crawler_type === 'creator' && (
            <Field label={t('field.creatorIds')} hint={t('field.creatorIdsHint')}>
              <textarea
                value={config.creator_ids}
                onChange={(e) => updateConfig({ creator_ids: e.target.value })}
                disabled={isDisabled}
                placeholder={t(`field.creatorIdsPlaceholder.${config.platform}`, t('field.creatorIdsPlaceholder.default'))}
                className="min-h-[60px] w-full rounded-md border border-cyber-border-DEFAULT bg-cyber-bg-tertiary px-3 py-2 text-xs font-mono text-cyber-text-primary placeholder:text-cyber-text-muted focus-visible:outline-none focus-visible:border-cyber-neon-cyan/50 focus-visible:shadow-cyber-soft disabled:cursor-not-allowed disabled:opacity-50 transition-all resize-none"
              />
              <ParsedIdList
                value={config.creator_ids}
                platform={config.platform}
                type="creator"
                disabled={isDisabled}
              />
              {config.platform === 'xhs' && (
                <div className="mt-2 rounded-lg border border-cyber-neon-orange/30 bg-cyber-neon-orange/5 p-2 text-[10px] leading-snug text-cyber-neon-orange font-mono">
                  {t('warning.xhsToken')}
                </div>
              )}
            </Field>
          )}

        </Group>

        <Group title={t('section.authMatrix.title')}>
          <Field label={t('field.loginMethod')}>
            <Select
              value={config.login_type}
              onValueChange={(value) => updateConfig({ login_type: value })}
              disabled={isDisabled}
            >
              <SelectTrigger className="h-9 text-xs">
                <SelectValue placeholder={t('field.loginMethodPlaceholder')} />
              </SelectTrigger>
              <SelectContent>
                {(options?.login_types || []).map((type) => (
                  <SelectItem key={type.value} value={type.value}>
                    {type.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          {config.login_type === 'cookie' ? (
            <Field label={t('field.cookies')} hint={t('field.cookiesHint')}>
              <textarea
                value={config.cookies}
                onChange={(e) => updateConfig({ cookies: e.target.value })}
                disabled={isDisabled}
                placeholder={t('field.cookiesPlaceholder')}
                className="min-h-[80px] w-full rounded-md border border-cyber-border-DEFAULT bg-cyber-bg-tertiary px-3 py-2 text-xs font-mono text-cyber-text-primary placeholder:text-cyber-text-muted focus-visible:outline-none focus-visible:border-cyber-neon-cyan/50 focus-visible:shadow-cyber-soft disabled:cursor-not-allowed disabled:opacity-50 transition-all resize-none"
              />
            </Field>
          ) : null}

          {config.login_type === 'cookie' && (config.platform === 'xhs' || config.platform === 'dy') ? (
            <div className="rounded-lg border border-cyber-neon-orange/30 bg-cyber-neon-orange/5 p-3 text-[11px] leading-snug text-cyber-neon-orange font-mono">
              {t('warning.cookieSlider')}
            </div>
          ) : null}
        </Group>

        <Group title={t('section.outputConfig.title')}>
          <Field label={t('field.saveFormat')}>
            <Select
              value={config.save_option}
              onValueChange={(value) => updateConfig({ save_option: value })}
              disabled={isDisabled}
            >
              <SelectTrigger className="h-9 text-xs">
                <SelectValue placeholder={t('field.saveFormatPlaceholder')} />
              </SelectTrigger>
              <SelectContent>
                {(options?.save_options || []).map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {t(`saveOption.${option.value}`, { defaultValue: option.label })}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {/* 已选的存储方式当前不可用（如 MySQL 挂了）：提前提示，不要等跑起来才报错 */}
            {saveOptionMissing && (
              <p className="text-[11px] font-mono text-cyber-neon-orange">
                {t('field.saveOptionUnavailable', { option: config.save_option })}
              </p>
            )}
          </Field>

          <div className="grid grid-cols-2 gap-1.5">
            <div className="flex items-center gap-2 rounded-md border border-cyber-border-subtle bg-cyber-bg-tertiary/30 p-1.5 hover:border-cyber-border-DEFAULT transition-colors">
              <Checkbox
                checked={config.enable_comments}
                onCheckedChange={(checked) => {
                  const isChecked = checked === true
                  updateConfig({
                    enable_comments: isChecked,
                    enable_sub_comments: isChecked ? config.enable_sub_comments : false,
                  })
                }}
                disabled={isDisabled}
              />
              <div className="flex items-center gap-2 min-w-0">
                <MessageSquare className="h-3.5 w-3.5 text-cyber-text-secondary flex-shrink-0" />
                <p className="text-[11px] font-mono text-cyber-text-primary truncate">
                  {t('field.commentExtraction')}
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2 rounded-md border border-cyber-border-subtle bg-cyber-bg-tertiary/30 p-1.5 hover:border-cyber-border-DEFAULT transition-colors">
              <Checkbox
                checked={config.enable_sub_comments}
                onCheckedChange={(checked) => updateConfig({ enable_sub_comments: checked === true })}
                disabled={isDisabled || !config.enable_comments}
              />
              <p className="text-[11px] font-mono text-cyber-text-primary truncate">
                {t('field.subComments')}
              </p>
            </div>

            <div className="flex items-center gap-2 rounded-md border border-cyber-border-subtle bg-cyber-bg-tertiary/30 p-1.5 hover:border-cyber-border-DEFAULT transition-colors">
              <Checkbox
                checked={config.enable_media}
                onCheckedChange={(checked) => updateConfig({ enable_media: checked === true })}
                disabled={isDisabled}
              />
              <div className="flex items-center gap-2 min-w-0">
                <ImageIcon className="h-3.5 w-3.5 text-cyber-text-secondary flex-shrink-0" />
                <p className="text-[11px] font-mono text-cyber-text-primary truncate">
                  {t('field.downloadMedia')}
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2 rounded-md border border-cyber-border-subtle bg-cyber-bg-tertiary/30 p-1.5 hover:border-cyber-border-DEFAULT transition-colors">
              <Checkbox
                checked={config.headless}
                onCheckedChange={(checked) => updateConfig({ headless: checked === true })}
                disabled={isDisabled}
              />
              <div className="flex items-center gap-2 min-w-0">
                <p className="text-[11px] font-mono text-cyber-text-primary truncate">
                  {t('field.headlessMode')}
                </p>
              </div>
            </div>
          </div>
        </Group>
      </div>

      {/* 底部磁吸操作条：固定在面板底部，不随表单滚动 */}
      <div className="flex-shrink-0 border-t border-cyber-border-subtle bg-cyber-bg-tertiary/40 p-2.5 space-y-1.5">
        <Button
          type="button"
          variant="outline"
          onClick={handleAddToQueue}
          disabled={addToQueueM.isPending || !currentTargets().length}
          className="h-8 w-full border-cyber-border-DEFAULT font-mono text-[11px] hover:border-cyber-neon-cyan/50 hover:text-cyber-neon-cyan transition-all"
        >
          {addToQueueM.isPending ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <ListPlus className="w-3.5 h-3.5" />
          )}
          {t('queue.addToQueue', '加入队列')}
        </Button>

        {isRunning ? (
          <Button
            onClick={handleStop}
            disabled={isBusy}
            className="w-full h-10 bg-cyber-neon-pink text-white font-mono font-bold text-sm tracking-wider hover:bg-cyber-neon-pink/90 hover:shadow-glow-pink-sm transition-all"
          >
            <Square className="w-4 h-4" />
            {isStopping ? t('button.stopping') : t('button.terminate')}
          </Button>
        ) : (
          <Button
            onClick={handleStart}
            disabled={isBusy}
            className="w-full h-10 bg-cyber-neon-cyan text-cyber-bg-primary font-mono font-bold text-sm tracking-wider hover:bg-cyber-neon-cyan/90 hover:shadow-glow-cyan-sm transition-all"
          >
            <Play className="w-4 h-4" />
            {isStarting ? t('button.initiating') : t('button.initiateScan')}
          </Button>
        )}
      </div>
    </div>
  )
}
