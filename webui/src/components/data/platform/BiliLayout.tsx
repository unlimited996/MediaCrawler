import { Eye, MessageSquare, ThumbsUp, Coins, Star, Play, ExternalLink } from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import {buildContent, pickMedia, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink, authorAnchor, TopicChips, CommentReplies, StanceBadge} from './shared'
import type { LayoutProps } from './XhsLayout'

interface BiliLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * B站详情页
 * 16:9 封面 + 时长角标 + 标题 + UP主行 + 播放/弹幕/点赞/硬币/收藏统计行 + 简介盒 + 评论
 */
export function BiliLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: BiliLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  const cover = d?.cover_url || media[0]?.url || ''
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  const stats = [
    { icon: Eye, label: t('manage.views'), value: d?.view_count },
    { icon: MessageSquare, label: t('manage.danmaku'), value: d?.danmaku_count },
    { icon: ThumbsUp, label: t('manage.likes'), value: d?.like_count },
    { icon: Coins, label: t('manage.coin'), value: d?.coin_count },
    { icon: Star, label: t('manage.favorite'), value: d?.collect_count },
  ].filter((s) => s.value != null)

  return (
    <div className="mx-auto max-w-3xl space-y-3">
      <article className="overflow-hidden rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel shadow-sm">
        {/* 封面 */}
        <div className="relative">
          {cover ? (
            <img
              src={cover}
              alt=""
              loading="lazy"
              className="aspect-video w-full object-cover"
            />
          ) : (
            <div className="flex aspect-video w-full items-center justify-center bg-cyber-bg-tertiary text-cyber-text-muted">
              <Play className="h-8 w-8" />
            </div>
          )}
          <span className="absolute right-2 bottom-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] text-white">
            {t('manage.video')}
          </span>
        </div>

        <div className="p-5">
          {/* 标题 */}
          <h2 className="text-base font-medium leading-snug text-cyber-text-primary hover:text-[color:var(--bili-accent)]">
            {d?.title || '-'}
          </h2>

          {/* UP 主行 */}
          <div className="mt-3 flex items-center gap-2">
            <AuthorLink url={d?.author_url} name={d?.author} avatar={d?.author_avatar} theme={theme} size={30} square />
            <span className="text-xs font-medium text-cyber-text-primary">
              {authorAnchor(d?.author_url, d?.author || '-')}
            </span>
            <span
              className="rounded px-1.5 py-0.5 text-[10px] font-medium text-white"
              style={{ background: theme.accent }}
            >
              {t('manage.up')}
            </span>
            <span className="ml-auto text-[11px] text-cyber-text-muted">
              {fmtTs(d?.publish_ts)}
            </span>
          </div>

          {/* 统计行 */}
          {stats.length > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-cyber-text-muted">
              {stats.map(({ icon: Icon, label, value }) => (
                <span key={label} className="flex items-center gap-1">
                  <Icon className="h-3.5 w-3.5" />
                  {fmtNum(value)} {label}
                </span>
              ))}
              <span className="flex items-center gap-1">
                <MessageSquare className="h-3.5 w-3.5" />
                {fmtNum(d?.comment_count ?? commentsTotal)} {t('manage.comment')}
              </span>
            </div>
          )}

          {/* 简介 */}
          {content && (
            <div className="mt-3 rounded-lg bg-cyber-bg-tertiary/60 p-3 text-xs leading-6 whitespace-pre-wrap text-cyber-text-secondary">
              {content}
            </div>
          )}

          {/* 标签 */}
          <TopicChips topics={tags} theme={theme} />

          {media.length > 1 && (
            <div className="mt-3 grid grid-cols-3 gap-2 sm:grid-cols-4">
              {media.slice(1).map((m, i) => (
                <a key={i} href={m.url} target="_blank" rel="noopener noreferrer">
                  <img
                    src={m.url}
                    alt=""
                    loading="lazy"
                    className="aspect-video w-full rounded border border-cyber-border-subtle object-cover"
                  />
                </a>
              ))}
            </div>
          )}

          {sourceUrl && (
            <a
              href={sourceUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-3 inline-flex items-center gap-1 text-[11px] text-cyber-text-muted hover:text-cyber-text-primary"
            >
              <ExternalLink className="h-3 w-3" />
              {t('manage.originLink')}
            </a>
          )}
        </div>
      </article>

      {/* 评论区 */}
      <section className="rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel p-5">
        <h3 className="text-sm font-medium text-cyber-text-primary">
          {t('manage.comments')}（{commentsTotal}）
        </h3>
        {commentsToolbar}
        {comments.length === 0 ? (
          <p className="mt-4 text-xs text-cyber-text-muted">{t('manage.noComments')}</p>
        ) : (
          <ul className="mt-3 space-y-4">
            {comments.map((c: any) => (
              <li key={c.id} className="flex gap-2.5">
                <AuthorLink url={c.author_url} name={c.author} avatar={c.author_avatar} theme={theme} size={28} square />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-medium text-cyber-text-primary">
                      {authorAnchor(c.author_url, c.author || '-')}
                    </span>
                    <span className="text-[11px] text-cyber-text-muted">{fmtTs(c.publish_ts)}</span>
                    <StanceBadge stance={c.stance} t={t} />
                  </div>
                  <div className="mt-1 text-sm leading-6 text-cyber-text-secondary"><RichText content={c.content} /></div>
                  <div className="mt-1 flex items-center gap-3 text-[11px] text-cyber-text-muted">
                    <span className="flex items-center gap-1">
                      <ThumbsUp className="h-3 w-3" />
                      {fmtNum(c.like_count)}
                    </span>
                    <span>{t('manage.reply')}</span>
                  </div>
                  <CommentReplies replies={c.replies} replyCount={c.reply_count} commentId={c.id} theme={theme} t={t} />
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
