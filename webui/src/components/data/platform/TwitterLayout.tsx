import { Heart, MessageCircle, Repeat2, BarChart2, ExternalLink } from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import {
  buildContent, pickMedia, MediaImage, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink,
  authorAnchor, TopicChips, CommentReplies, StanceBadge,
} from './shared'
import type { LayoutProps } from './XhsLayout'

interface TwitterLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * X（Twitter）详情页
 *
 * 对齐 X 原生推文页：无重卡片、靠细分隔线分层；头像列 + 内容列两栏；
 * 正文 15px 大字号；底部 reply / repost / like / views 四枚图标等距横向铺开；
 * 评论区为「回复流」，左侧竖线表示回复归属（X 的 thread line）。
 */
export function TwitterLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: TwitterLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  const gallery = media.slice(0, 4).map((m) => m.candidates)
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  const handle = (d?.author || 'user').replace(/\s+/g, '').toLowerCase()
  const actionBase = 'flex items-center gap-1.5 text-[13px] text-cyber-text-muted transition-colors'

  return (
    <div className="mx-auto max-w-2xl">
      {/* 推文主体：X 风格，细边框 + 大留白 */}
      <article className="rounded-2xl border border-cyber-border-subtle bg-cyber-bg-panel px-4 pt-3 pb-1">
        {/* 头像列 + 内容列 */}
        <div className="flex gap-3">
          <AuthorLink url={d?.author_url} name={d?.author} avatar={d?.author_avatar} theme={theme} size={40} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="truncate text-[15px] font-bold text-cyber-text-primary">
                {authorAnchor(d?.author_url, d?.author || t('manage.author'))}
              </span>
              <span className="truncate text-[13px] text-cyber-text-muted">@{handle}</span>
              <span className="text-[13px] text-cyber-text-muted">·</span>
              <span className="whitespace-nowrap text-[13px] text-cyber-text-muted">{fmtTs(d?.publish_ts)}</span>
              <span
                className="ml-auto flex-shrink-0 rounded-full px-2 py-[1px] text-[11px] font-medium"
                style={{ color: theme.accent, background: theme.accentSoft }}
              >
                {theme.label}
              </span>
            </div>

            {d?.title && (
              <h2 className="mt-1.5 break-anywhere text-[15px] font-bold leading-snug text-cyber-text-primary">
                {d.title}
              </h2>
            )}

            {content && (
              <div className="mt-1 whitespace-pre-wrap text-[15px] leading-7 text-cyber-text-primary">
                <RichText content={content} />
              </div>
            )}

            {/* 媒体：X 是 1~4 张自适应网格，圆角 2xl 描边 */}
            {media.length > 0 && (
              <div
                className={`mt-3 grid gap-0.5 overflow-hidden rounded-2xl border border-cyber-border-subtle ${
                  media.length === 1 ? 'grid-cols-1' : 'grid-cols-2'
                }`}
              >
                {media.slice(0, 4).map((m, i) => (
                  <MediaImage
                    key={i}
                    media={m}
                    gallery={gallery}
                    galleryIndex={i}
                    className={`w-full cursor-zoom-in border border-cyber-border-subtle object-cover ${
                      media.length === 1 ? 'max-h-96' : 'aspect-square'
                    }`}
                  />
                ))}
              </div>
            )}

            {/* 话题 hashtag：X 用蓝色行内样式 */}
            <TopicChips topics={tags} theme={theme} />

            {/* 互动行：四枚图标等距，数字跟在图标右侧（X 的 action bar） */}
            <div className="mt-3 flex max-w-md items-center justify-between pb-2 pr-8">
              <span className={`${actionBase} hover:text-cyber-neon-cyan`}>
                <MessageCircle className="h-4 w-4" />
                {fmtNum(d?.comment_count ?? commentsTotal)}
              </span>
              <span className={`${actionBase} hover:text-cyber-neon-green`}>
                <Repeat2 className="h-4 w-4" />
                {fmtNum(d?.share_count)}
              </span>
              <span
                className={`${actionBase} hover:text-cyber-neon-pink`}
                style={{ color: (d?.like_count ?? 0) > 0 ? theme.accent : undefined }}
              >
                <Heart className="h-4 w-4" />
                {fmtNum(d?.like_count)}
              </span>
              <span className={`${actionBase} hover:text-cyber-neon-cyan`}>
                <BarChart2 className="h-4 w-4" />
                {fmtNum(d?.view_count ?? d?.heat_score)}
              </span>
            </div>
          </div>
        </div>

        {sourceUrl && (
          <a
            href={sourceUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="mb-2 ml-[52px] inline-flex items-center gap-1 text-[13px] text-cyber-text-muted hover:underline"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            {t('manage.originLink')}
          </a>
        )}
      </article>

      {/* 评论区：回复流，靠分隔线与竖线区分层级 */}
      <section className="mt-3 rounded-2xl border border-cyber-border-subtle bg-cyber-bg-panel px-4 py-3">
        <h3 className="text-[15px] font-bold text-cyber-text-primary">
          {t('manage.comments')}（{commentsTotal}）
        </h3>
        {commentsToolbar}
        {comments.length === 0 ? (
          <p className="mt-4 text-[13px] text-cyber-text-muted">{t('manage.noComments')}</p>
        ) : (
          <ul className="mt-2 divide-y divide-cyber-border-subtle">
            {comments.map((c: any) => (
              <li key={c.id} className="flex gap-3 py-3">
                <AuthorLink url={c.author_url} name={c.author} avatar={c.author_avatar} theme={theme} size={32} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[14px] font-bold text-cyber-text-primary">
                      {authorAnchor(c.author_url, c.author || '-')}
                    </span>
                    <span className="text-[13px] text-cyber-text-muted">
                      @{String(c.author || 'user').replace(/\s+/g, '').toLowerCase()}
                    </span>
                    <span className="text-[13px] text-cyber-text-muted">· {fmtTs(c.publish_ts)}</span>
                    <StanceBadge stance={c.stance} t={t} tagCode={c.stance_tag_code} tagName={c.stance_tag_name} tagColor={c.stance_tag_color} comment={c} />
                  </div>
                  <div className="mt-0.5 text-[15px] leading-7 text-cyber-text-primary">
                    <RichText content={c.content} />
                  </div>
                  <div className="mt-1 flex max-w-sm items-center justify-between text-[13px] text-cyber-text-muted">
                    <span className="flex items-center gap-1.5">
                      <MessageCircle className="h-3.5 w-3.5" />
                      {fmtNum(c.reply_count)}
                    </span>
                    <span className="flex items-center gap-1.5">
                      <Heart className="h-3.5 w-3.5" />
                      {fmtNum(c.like_count)}
                    </span>
                  </div>
                  {/* X 的回复用左侧竖线串起来 */}
                  <CommentReplies
                    replies={c.replies} replyCount={c.reply_count} commentId={c.id}
                    theme={theme} t={t}
                    containerClassName="mt-2 space-y-2 border-l-2 border-cyber-border-subtle pl-3"
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
