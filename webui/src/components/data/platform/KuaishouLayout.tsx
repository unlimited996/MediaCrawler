import { Heart, MessageSquare, Star, Share2, Play, ExternalLink } from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import {buildContent, pickMedia, MediaImage, pickCoverMedia, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink, authorAnchor, TopicChips, CommentReplies, StanceBadge} from './shared'
import type { LayoutProps } from './XhsLayout'

interface KuaishouLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * 快手详情页
 * 橙红主色 + 竖版封面居中 + 标题 + 作者行 + 点赞/评论/收藏/分享四联统计 + 评论
 */
export function KuaishouLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: KuaishouLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  // 封面候选源：cover_local → cover_url → cover_backup(S3) → 首图，逐个回退
  const cover = pickCoverMedia(d, media)
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  const stats = [
    { icon: Heart, label: t('manage.likes'), value: d?.like_count },
    { icon: MessageSquare, label: t('manage.comment'), value: d?.comment_count ?? commentsTotal },
    { icon: Star, label: t('manage.favorite'), value: d?.collect_count },
    { icon: Share2, label: t('manage.share'), value: d?.share_count },
  ].filter((s) => s.value != null)

  return (
    <div className="mx-auto max-w-2xl space-y-3">
      <article className="overflow-hidden rounded-2xl border border-cyber-border-subtle bg-cyber-bg-panel shadow-sm">
        {/* 竖版封面 */}
        <div className="relative mx-auto w-full max-w-[16rem]">
          {cover ? (
            <MediaImage media={cover} className="aspect-[3/4] w-full object-cover" />
          ) : (
            <div className="flex aspect-[3/4] w-full items-center justify-center bg-cyber-bg-tertiary text-cyber-text-muted">
              <Play className="h-8 w-8" />
            </div>
          )}
          <span className="absolute right-2 bottom-2 rounded bg-black/50 px-1.5 py-0.5 text-[10px] text-white">
            {t('manage.video')}
          </span>
        </div>

        <div className="p-5">
          {/* 标题 */}
          {d?.title && (
            <h2 className="break-anywhere text-base font-bold leading-snug text-cyber-text-primary">
              {d.title}
            </h2>
          )}

          {/* 作者行 */}
          <div className="mt-3 flex items-center gap-2.5">
            <AuthorLink url={d?.author_url} name={d?.author} avatar={d?.author_avatar} theme={theme} size={30} />
            <span className="truncate text-xs font-medium text-cyber-text-primary">
              {authorAnchor(d?.author_url, d?.author || '-')}
            </span>
            <span className="ml-auto text-[11px] text-cyber-text-muted">
              {fmtTs(d?.publish_ts)}
            </span>
          </div>

          {/* 正文 */}
          {content && (
            <p className="mt-2 line-clamp-4 whitespace-pre-wrap text-sm leading-6 text-cyber-text-secondary">
              {content}
            </p>
          )}

          {/* 标签 */}
          <TopicChips topics={tags} theme={theme} />

          {/* 四联统计 */}
          {stats.length > 0 && (
            <div className="mt-4 grid grid-cols-4 gap-2 border-t border-cyber-border-subtle pt-3">
              {stats.map(({ icon: Icon, label, value }) => (
                <div key={label} className="flex flex-col items-center gap-1">
                  <Icon className="h-4 w-4" style={{ color: theme.accent }} />
                  <span className="text-xs font-medium text-cyber-text-primary">
                    {fmtNum(value)}
                  </span>
                  <span className="text-[10px] text-cyber-text-muted">{label}</span>
                </div>
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
      <section className="rounded-2xl border border-cyber-border-subtle bg-cyber-bg-panel p-5">
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
                <AuthorLink url={c.author_url} name={c.author} avatar={c.author_avatar} theme={theme} size={28} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5 text-xs text-cyber-text-muted">
                    {authorAnchor(c.author_url, c.author || '-')}
                    <StanceBadge stance={c.stance} t={t} tagCode={c.stance_tag_code} tagName={c.stance_tag_name} tagColor={c.stance_tag_color} comment={c} />
                  </div>
                  <div className="mt-0.5 text-sm leading-6 text-cyber-text-primary"><RichText content={c.content} /></div>
                  <div className="mt-1 flex items-center gap-3 text-[11px] text-cyber-text-muted">
                    <span>{fmtTs(c.publish_ts)}</span>
                    <span className="flex items-center gap-1">
                      <Heart className="h-3 w-3" />
                      {fmtNum(c.like_count)}
                    </span>
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
