# -*- coding: utf-8 -*-
"""ENTITLEMENT 批次 5+6 远程验收：食材配额（402/409）+ 柴豆兑换会员全套语义。

对生产 library 服务跑。设计（单设备全链路，真值用 DB 回查）：
  批次 5  Lv1 食材上限 10：第 11 条 402 INGREDIENT_QUOTA_EXCEEDED；同名 409
  批次 6  ① 0 豆兑换 → 402 COINS_INSUFFICIENT
          ② SQL 调账 +100 豆（admin_adjust 流水）
          ③ 兑换 vip_7d(50) → coinsLeft=50；真值 coins=50（不双重扣减）；
             member.active=true；entitlements 满级(50/10)；流水 coins_spend 恰 1 条 -50
          ④ 会员期食材上限升 50：第 11 条食材成功
          ⑤ 叠加兑换 vip_1d(10) → until 顺延 +1d（= until2-until1 ≈ 24h）
          ⑥ SQL 到期回退 → active=false、entitlements 回落 Lv1(10/3)、第 12 条食材 402
          ⑦ 余额 40 < vip_365d(1200) → 402

DB 调账走 admin_adjust（growth_event CHECK 的合法枚举），只作用于本次验收设备。
公网链路对 POST 不稳：每次请求全新连接 + 3 次重试，以 membership 真值为判据。
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime

import httpx
import psycopg2

BASE = "http://www.joyeuse.cn:18081/library/api"
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(f"{name}  {detail}")
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def req(method: str, url: str, **kw):
    last = None
    for _ in range(3):
        try:
            with httpx.Client(timeout=40.0, trust_env=False) as c:
                return c.request(method, url, **kw)
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def jreq(method: str, url: str, **kw):
    r = None
    for _ in range(3):
        r = req(method, url, **kw)
        try:
            return r, r.json()
        except Exception:
            time.sleep(2)
    return r, {}


def db(sql: str, args: tuple = ()) -> list:
    """生产库直连（公网断连重试）。"""
    last = None
    for _ in range(3):
        try:
            conn = psycopg2.connect(
                host=os.environ["KH_DB_HOST"], port=4321, user="im",
                password=os.environ["KH_DB_PASS"], dbname="imdb",
                connect_timeout=20, sslmode="prefer")
            try:
                cur = conn.cursor()
                cur.execute(sql, args)
                rows = cur.fetchall() if cur.description else []
                conn.commit()
                return rows
            finally:
                conn.close()
        except psycopg2.OperationalError as e:
            # 已提交的 DDL/DML 报断连时先回查确认是否实际生效
            if "server closed the connection" in str(e) and cur_probe(sql, args):
                return []
            last = e
            time.sleep(2)
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def cur_probe(sql: str, args: tuple) -> bool:
    """断连后用只读查询确认写入是否已生效（简化：仅对 UPDATE/INSERT 的调用点）。"""
    return False  # 由调用处自行处理回查，这里不通用化


def membership(device_id: str) -> dict:
    _, m = jreq("GET", f"{BASE}/membership", params={"deviceId": device_id})
    return m


def add_ingredient(device_id: str, name: str):
    return jreq("POST", f"{BASE}/fridge/ingredients",
                json={"deviceId": device_id, "name": name,
                      "category": "验收", "plan": "不设提醒"})


def spend(device_id: str, sku: str):
    return jreq("POST", f"{BASE}/membership/coins/spend",
                json={"deviceId": device_id, "sku": sku})


def parse_until(s: str) -> datetime:
    return datetime.fromisoformat(s)


def main() -> int:
    dev = "accept-mem-" + uuid.uuid4().hex[:12]
    print(f"验收设备: {dev}")

    # ---------- 注册 + 初始态 ----------
    r, _ = jreq("POST", f"{BASE}/auth/devices", params={"deviceId": dev})
    check("注册: 200", r.status_code == 200, f"http={r.status_code}")
    m = membership(dev)
    check("初始态: 0/0/Lv1 且 member.active=false",
          m.get("points") == 0 and m.get("coins") == 0 and m.get("level") == 1
          and m.get("member", {}).get("active") is False,
          f"m={m.get('points')}/{m.get('coins')}/{m.get('level')} "
          f"member={m.get('member')}")

    # ---------- 批次 5：食材配额（Lv1 上限 10） ----------
    for i in range(1, 11):
        r, _ = add_ingredient(dev, f"验收食材{i:02d}")
        if r.status_code != 200:
            check(f"食材: 第 {i} 条 200", False, f"http={r.status_code}")
            break
    else:
        check("食材: 前 10 条全部 200", True)
    r, rj = add_ingredient(dev, "验收食材11")
    check("食材: 第 11 条 402 QUOTA_EXCEEDED",
          r.status_code == 402 and
          rj.get("detail", {}).get("code") == "INGREDIENT_QUOTA_EXCEEDED",
          f"http={r.status_code} {r.text[:60]}")
    r, _ = add_ingredient(dev, "验收食材01")
    check("食材: 同名 409", r.status_code == 409, f"http={r.status_code}")

    # ---------- 批次 6-①：0 豆兑换 → 402 ----------
    r, rj = spend(dev, "vip_1d")
    det = rj.get("detail", {})
    check("兑换: 0 豆不足 402 COINS_INSUFFICIENT",
          r.status_code == 402 and det.get("code") == "COINS_INSUFFICIENT",
          f"http={r.status_code} detail={str(det)[:70]}")

    # ---------- 批次 6-②：SQL 调账 +100 豆 ----------
    ref = "accept-" + uuid.uuid4().hex[:12]
    db("INSERT INTO kitchenhelper.growth_event "
       "(device_id, event_type, ref_id, points, coins) "
       "VALUES (%s, 'admin_adjust', %s, 0, 100)", (dev, ref))
    db("INSERT INTO kitchenhelper.membership_state (device_id, points, coins) "
       "VALUES (%s, 0, 100) ON CONFLICT (device_id) "
       "DO UPDATE SET coins = kitchenhelper.membership_state.coins + 100, "
       "updated_at = now()", (dev,))
    m = membership(dev)
    check("调账: 柴豆真值 100", m.get("coins") == 100, f"coins={m.get('coins')}")

    # ---------- 批次 6-③：兑换 vip_7d ----------
    r, rj = spend(dev, "vip_7d")
    ok = (r.status_code == 200 and rj.get("ok") is True
          and rj.get("coinsLeft") == 50 and rj.get("cost") == 50)
    check("兑换: vip_7d(50豆) 200 coinsLeft=50", ok,
          f"http={r.status_code} {str(rj)[:80]}")
    until1_raw = rj.get("memberUntil", "")
    until1 = parse_until(until1_raw)
    check("兑换: memberUntil ≈ 7 天后",
          abs((until1 - datetime.now(until1.tzinfo)).total_seconds()
              - 7 * 86400) < 600,
          f"until={until1_raw}")

    m = membership(dev)
    ent = m.get("entitlements", {})
    check("真值: coins=50（不双重扣减）", m.get("coins") == 50,
          f"coins={m.get('coins')}")
    check("真值: member.active=true + until 下发",
          m.get("member", {}).get("active") is True and m.get("member", {}).get("until"),
          f"member={m.get('member')}")
    check("真值: 权益满级口径 50/10",
          ent.get("customIngredientLimit") == 50 and ent.get("ocrDailyLimit") == 10,
          f"ent={ent}")
    rows = db("SELECT count(*), sum(coins) FROM kitchenhelper.growth_event "
              "WHERE device_id = %s AND event_type = 'coins_spend'", (dev,))
    check("流水: coins_spend 恰 1 条 -50",
          rows[0] == (1, -50), f"rows={rows}")

    # ---------- 批次 6-④：会员期食材上限升 50 ----------
    r, _ = add_ingredient(dev, "验收食材11会员期")
    check("会员期: 第 11 条食材 200（上限 50）", r.status_code == 200,
          f"http={r.status_code}")

    # ---------- 批次 6-⑤：叠加兑换顺延 ----------
    time.sleep(2)
    r, rj = spend(dev, "vip_1d")
    ok = (r.status_code == 200 and rj.get("coinsLeft") == 40
          and rj.get("cost") == 10)
    check("叠加: vip_1d(10豆) 200 coinsLeft=40", ok,
          f"http={r.status_code} {str(rj)[:80]}")
    until2 = parse_until(rj.get("memberUntil", ""))
    delta_h = (until2 - until1).total_seconds() / 3600
    check("叠加: 到期日顺延 ≈ 24h", abs(delta_h - 24) < 0.2,
          f"delta={delta_h:.2f}h")
    m = membership(dev)
    check("叠加真值: coins=40", m.get("coins") == 40, f"coins={m.get('coins')}")

    # ---------- 批次 6-⑥：SQL 到期回退 ----------
    db("UPDATE kitchenhelper.membership_grant "
       "SET expire_at = now() - interval '1 hour' WHERE device_id = %s", (dev,))
    m = membership(dev)
    ent = m.get("entitlements", {})
    check("到期: member.active=false 且权益回落 Lv1(10/3)",
          m.get("member", {}).get("active") is False
          and ent.get("customIngredientLimit") == 10
          and ent.get("ocrDailyLimit") == 3,
          f"member={m.get('member')} ent={ent}")
    r, rj = add_ingredient(dev, "验收食材12回落后")
    check("到期: 第 12 条食材 402（used=11>10）",
          r.status_code == 402 and
          rj.get("detail", {}).get("code") == "INGREDIENT_QUOTA_EXCEEDED",
          f"http={r.status_code}")

    # ---------- 批次 6-⑦：余额 40 < 1200 ----------
    r, rj = spend(dev, "vip_365d")
    check("兑换: 40<1200 → 402 COINS_INSUFFICIENT",
          r.status_code == 402 and
          rj.get("detail", {}).get("code") == "COINS_INSUFFICIENT",
          f"http={r.status_code}")

    print()
    print(f"===== 验收结果: PASS={len(PASSED)} FAIL={len(FAILED)} =====")
    if FAILED:
        print("失败项:")
        for f in FAILED:
            print(f"  {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
