# -*- coding: utf-8 -*-
"""抓取队列集成测试：用假子进程替换 Popen，验证排队 / 自动续跑 / 终止的完整链路。

不真的拉起浏览器，跑得快且结果确定。
"""
import asyncio
import os
import sys

sys.path.insert(0, r"D:\workspace\MediaCrawler")

import importlib  # noqa: E402

# 注意 api/services/__init__.py 把 crawler_manager 这个单例实例导成了同名属性，
# 会遮住子模块本身，所以这里必须按模块名取，才能 patch 到模块级的 subprocess
cm = importlib.import_module("api.services.crawler_manager")  # noqa: E402
from api.schemas import CrawlerStartRequest  # noqa: E402
from api.services import crawl_queue, crawler_manager as mgr  # noqa: E402

CMDS = []


class FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        if STICKY["on"]:
            # 假进程不退出时，别让 read 循环空转把 CPU 打满
            import time
            time.sleep(0.05)
            return ""
        return self._lines.pop(0) if self._lines else ""

    def read(self):
        return ""


# 置为 True 时，假进程永远不会自己退出（用来测"终止"这个分支）
STICKY = {"on": False}


class FakeProcess:
    """poll 前 2 次返回 None（模拟在跑），之后返回退出码，让 read task 快速收尾。"""

    def __init__(self, lines=("fake line 1", "fake line 2"), exit_code=0):
        self.stdout = FakeStdout(lines)
        self.returncode = exit_code
        self._ticks = 0
        self.killed = False

    def poll(self):
        if self.killed:  # 收到 SIGTERM / kill 后立刻退出，免得测试等满 15 秒
            return self.returncode
        if STICKY["on"]:
            return None
        self._ticks += 1
        return None if self._ticks <= 2 else self.returncode

    def send_signal(self, sig):
        self.killed = True

    def kill(self):
        self.killed = True


def req(url: str, platform: str = "xhs", save: str = "jsonl") -> CrawlerStartRequest:
    return CrawlerStartRequest(platform=platform, login_type="cookie",
                               crawler_type="detail", specified_ids=url,
                               save_option=save, headless=True)


async def wait_until(pred, timeout=8.0, interval=0.1):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if await pred() if asyncio.iscoroutinefunction(pred) else pred():
            return True
        await asyncio.sleep(interval)
    return False


async def main():
    # 替换子进程与两个外部依赖，让测试不碰真实环境
    async def _no_cookie(platform):
        return None

    async def _no_storage_error(save_option):
        return None

    cm.subprocess.Popen = lambda cmd, **kw: (CMDS.append(cmd), FakeProcess())[1]
    mgr._get_pool_cookie = _no_cookie              # type: ignore[assignment]
    mgr._check_storage_available = _no_storage_error  # type: ignore[assignment]

    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' -> ' + str(extra)) if extra else ''}")

    print("== 1) 空闲时提交：直接开跑 ==")
    r1 = await mgr.start(req("https://xhs.test/a"))
    check("返回 started", r1["mode"] == "started", r1)
    check("子进程已拉起", mgr.is_running())
    check("命令行带了 URL", any("https://xhs.test/a" in c for c in CMDS[-1]), CMDS[-1][-2:])

    print("== 2) 忙时提交：自动入队而不是拒绝 ==")
    r2 = await mgr.start(req("https://xhs.test/b"))
    check("返回 queued", r2["mode"] == "queued", r2)
    stats = await crawl_queue.stats()
    check("队列里有 1 条待执行", stats["pending"] == 1, stats)
    check("没有并发起第二个进程", len(CMDS) == 1, len(CMDS))

    print("== 3) 队列中再追加 2 条（含 1 条重复） ==")
    add = await crawl_queue.add(["https://xhs.test/c", "https://xhs.test/b"],
                                "xhs", "detail", {"save_option": "jsonl"})
    check("新增 1 条", len(add["added"]) == 1, add["added"])
    check("去重 1 条", len(add["duplicates"]) == 1, add["duplicates"])

    print("== 4) 从队列里删掉一条（用户要的删除能力） ==")
    snap = await crawl_queue.snapshot(statuses=["pending"])
    victim = snap[0].id
    removed = await crawl_queue.remove([victim])
    check("删除生效", removed == [victim], removed)
    stats = await crawl_queue.stats()
    check("待执行减到 1", stats["pending"] == 1, stats)

    print("== 5) 前一个跑完 → 自动取队列下一批 ==")
    drained = await wait_until(lambda: len(CMDS) >= 2, timeout=10)
    check("自动续跑了第二批", drained, f"共启动 {len(CMDS)} 次")
    if drained:
        check("第二批命令行带队列里的 URL",
              any("https://xhs.test/" in c for c in CMDS[1]), CMDS[1][-2:])
        check("第一批任务已标记完成",
              len(await crawl_queue.snapshot(statuses=["done"])) >= 1,
              [i.url for i in await crawl_queue.snapshot(statuses=["done"])])

    print("== 6) 跑完最后一条 → 队列清空、状态回到 idle ==")
    idle = await wait_until(lambda: mgr.status == "idle", timeout=10)
    check("状态回到 idle", idle, mgr.status)
    stats = await crawl_queue.stats()
    check("没有 pending 残留", stats["pending"] == 0, stats)
    check("没有 running 残留", stats["running"] == 0, stats)

    print("== 7) 终止：暂停续跑 + 进行中标 cancelled ==")
    # 一次加 7 条（> batch_size=5）：只会取走前 5 条，剩下 2 条要留在队列里。
    # 开 STICKY 让假进程不自己退出，才能观察到"跑着的时候点终止"这个状态
    await crawl_queue.add([f"https://xhs.test/stop{i}" for i in range(7)], "xhs", "detail",
                          {"save_option": "jsonl"})
    STICKY["on"] = True
    await mgr.start_queue()
    check("手动 resume 能起任务", mgr.is_running())
    before = await crawl_queue.stats()
    check("按批大小取走 5 条", before["running"] == 5 and before["pending"] == 2, before)
    await mgr.stop()
    stats = await crawl_queue.stats()
    check("进行中的被标 cancelled", stats["cancelled"] == 5, stats)
    check("剩余 pending 保留在队列", stats["pending"] == 2, stats)
    check("续跑开关已关闭", crawl_queue.auto_run is False)
    check("状态回 idle", mgr.status == "idle", mgr.status)

    print("== 8) 失败任务可重试 ==")
    snap = await crawl_queue.snapshot(statuses=["cancelled"])
    rq = await crawl_queue.requeue([i.id for i in snap])
    check("已重新排队", len(rq) >= 1, rq)

    print("== 9) 清空队列 ==")
    cl = await crawl_queue.clear()
    check("清空完毕", len(await crawl_queue.snapshot()) == 0, cl)

    print("\n===== 结果:", "全部通过" if ok else "存在失败", "=====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
