import { Heart, MessageSquare, Star, Share2, Play, Music } from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import {buildContent, pickMedia, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink, authorAnchor, TopicChips, CommentReplies, StanceBadge} from './shared'
import type { LayoutProps } from './XhsLayout'

interface DouyinLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * 抖音详情页
 * 深色舞台（抖音播放器 UI）+ 竖版封面 + 右侧点赞/评论/收藏/分享操作栏 + 深色评论卡
 */
export function DouyinLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: DouyinLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  const cover = d?.cover_url || media[0]?.url || ''
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  const railItem = (
    icon: React.ReactNode,
    label: string,
    value?: number | string | null,
  ) => (
    <div className="flex flex-col items-center gap-1 text-white">
      <span className="flex h-9 w-9 items-center justify-center rounded-full bg-white/10">
        {icon}
      </span>
      <span className="text-[11px]">{value != null ? fmtNum(value) : label}</span>
    </div>
  )

  return (
    <div className="mx-auto max-w-3xl space-y-3">
      {/* 深色播放卡 */}
      <article
        className="overflow-hidden rounded-2xl border border-white/5 shadow-lg"
        style={{ background: '#161823' }}
      >
        <div className="flex gap-4 p-4">
          {/* 竖版封面 */}
          <div className="relative w-44 flex-shrink-0 sm:w-52">
            {cover ? (
              <img
                src={cover}
                alt=""
                loading="lazy"
                className="aspect-[3/4] w-full rounded-xl object-cover"
              />
            ) : (
              <div className="flex aspect-[3/4] w-full items-center justify-center rounded-xl bg-white/5 text-white/30">
                <Play className="h-8 w-8" />
              </div>
            )}
            <span className="absolute left-2 top-2 flex items-center gap-1 rounded bg-black/50 px-1.5 py-0.5 text-[10px] text-white">
              <Play className="h-2.5 w-2.5" />
              {t('manage.video')}
            </span>
          </div>

          {/* 文案区 */}
          <div className="min-w-0 flex-1 text-white">
            <div className="flex items-center gap-2">
              <AuthorLink url={d?.author_url} name={d?.author} avatar={d?.author_avatar} theme={theme} size={26} square />
              <span className="truncate text-xs text-white/80">@{authorAnchor(d?.author_url, d?.author || '-')}</span>
              <span className="ml-auto text-[10px] text-white/40">{theme.label}</span>
            </div>
            {d?.title && (
              <h2 className="mt-2.5 text-[15px] font-medium leading-snug">{d.title}</h2>
            )}
            {content && (
              <p className="mt-1.5 line-clamp-4 whitespace-pre-wrap text-xs leading-6 text-white/60">
                {content}
              </p>
            )}
            <TopicChips topics={tags} theme={theme} />
            <div className="mt-2 flex items-center gap-1.5 text-[11px] text-white/40">
              <Music className="h-3 w-3" />
              <span className="truncate">{fmtTs(d?.publish_ts)}</span>
            </div>
          </div>

          {/* 右侧操作栏 */}
          <div className="flex flex-col items-center gap-4">
            {railItem(<Heart className="h-4 w-4" style={{ color: theme.accent }} />, t('manage.likes'), d?.like_count)}
            {railItem(<MessageSquare className="h-4 w-4" />, t('manage.comment'), d?.comment_count ?? commentsTotal)}
            {railItem(<Star className="h-4 w-4" />, t('manage.favorite'), d?.collect_count)}
            {railItem(<Share2 className="h-4 w-4" />, t('manage.share'), d?.share_count)}
          </div>
        </div>
      </article>

      {/* 评论区（深色） */}
      <section
        className="rounded-2xl border border-white/5 p-5"
        style={{ background: '#161823' }}
      >
        <h3 className="text-sm font-medium text-white">
          {t('manage.comments')}（{commentsTotal}）
        </h3>
        {commentsToolbar}
        {comments.length === 0 ? (
          <p className="mt-4 text-xs text-white/40">{t('manage.noComments')}</p>
        ) : (
          <ul className="mt-3 space-y-4">
            {comments.map((c: any) => (
              <li key={c.id} className="flex gap-2.5">
                <AuthorLink url={c.author_url} name={c.author} avatar={c.author_avatar} theme={theme} size={28} square />
                <div className="min-w-0 flex-1">
                  <div className="text-xs text-white/50">{authorAnchor(c.author_url, c.author || '-')}</div>
                  <div className="mt-0.5 text-sm leading-6 text-white/90"><RichText content={c.content} /></div>
                  <div className="mt-1 flex items-center gap-3 text-[11px] text-white/40">
                    <span>{fmtTs(c.publish_ts)}</span>
                    <StanceBadge stance={c.stance} t={t} dark />
                    <span className="flex items-center gap-1">
                      <Heart className="h-3 w-3" />
                      {fmtNum(c.like_count)}
                    </span>
                  </div>
                  <CommentReplies
                    replies={c.replies} replyCount={c.reply_count} commentId={c.id}
                    theme={theme}
                    t={t}
                    containerClassName="mt-2 space-y-1.5 rounded-lg bg-white/5 p-2.5"
                    mutedClassName="text-white/50"
                    textClassName="text-white/70"
                    dark
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {sourceUrl && (
        <a
          href={sourceUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-[11px] text-white/40 hover:text-white/70"
        >
          {t('manage.originLink')}
        </a>
      )}
    </div>
  )
}
