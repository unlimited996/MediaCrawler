# -*- coding: utf-8 -*-
"""ENTITLEMENT 远程接口验收脚本（对生产 library 服务跑）。

公网链路对 POST 不稳（断连/双发），故：
- 每次请求用全新连接 + 最多 3 次重试（幂等键/clientLogId/服务端配额保证安全）；
- 发分判据以 membership 真值为准（awarded 字段可能是重试那次幂等拒绝的 0）。
"""
from __future__ import annotations

import hashlib
import hmac
import sys
import time
import uuid

import httpx

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


def membership(device_id: str) -> dict:
    _, m = jreq("GET", f"{BASE}/membership", params={"deviceId": device_id})
    return m


def main() -> int:
    claimed_id = "accept-" + uuid.uuid4().hex[:16]

    # 批次 2：设备注册
    r, reg = jreq("POST", f"{BASE}/auth/devices", params={"deviceId": claimed_id})
    check("注册: 存量 UUID claim 200", r.status_code == 200, f"http={r.status_code}")
    secret = reg.get("deviceSecret", "")
    check("注册: deviceSecret(64hex)", len(secret) == 64, f"len={len(secret)}")
    check("注册: deviceId 保持原值", reg.get("deviceId") == claimed_id)

    # 批次 3：membership 初始态
    m = membership(claimed_id)
    check("membership: 初始 0/0/Lv1",
          m.get("points") == 0 and m.get("coins") == 0 and m.get("level") == 1,
          f"points={m.get('points')} coins={m.get('coins')} level={m.get('level')}")
    ent = m.get("entitlements", {})
    check("membership: 权益公式(Lv1: 食材10/OCR3)",
          ent.get("customIngredientLimit") == 10 and ent.get("ocrDailyLimit") == 3,
          f"ent={ent}")

    # 批次 3：发分 + 幂等（真值判定）
    log_id = uuid.uuid4().hex
    body = {
        "deviceId": claimed_id, "eatenOn": time.strftime("%Y-%m-%d"),
        "meal": "dinner", "note": "ENTITLEMENT 验收",
        "recipeIds": ["炒菜/家常土豆片", "汤/牛筋丸汤"],
        "clientLogId": log_id,
    }
    r1, _ = jreq("POST", f"{BASE}/meal-log", json=body)
    check("meal-log: 首次 200", r1.status_code == 200, f"http={r1.status_code}")
    m = membership(claimed_id)
    check("meal-log: 首次发分真值 20/4",
          m.get("points") == 20 and m.get("coins") == 4,
          f"真值 {m.get('points')}/{m.get('coins')}")

    r2, a2 = jreq("POST", f"{BASE}/meal-log", json=body)
    m = membership(claimed_id)
    check("meal-log: 同 clientLogId 重放不重复发分",
          r2.status_code == 200 and m.get("points") == 20,
          f"awarded={a2.get('awarded')} 真值={m.get('points')}")

    body2 = dict(body, clientLogId=uuid.uuid4().hex,
                recipeIds=["炒菜/家常土豆片"])
    jreq("POST", f"{BASE}/meal-log", json=body2)
    m = membership(claimed_id)
    check("meal-log: 同菜二次只 +10（同菜终身一次）",
          m.get("points") == 30 and m.get("coins") == 6,
          f"真值 {m.get('points')}/{m.get('coins')}")

    # 批次 2：签名冒烟（灰度期不强拒）
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    raw = f"POST\n/meal-log\n{hashlib.sha256(b'').hexdigest()}\n{nonce}\n{ts}"
    sig = hmac.new(bytes.fromhex(secret), raw.encode(), hashlib.sha256).hexdigest()
    r = req("POST", f"{BASE}/meal-log",
            json={"deviceId": claimed_id, "eatenOn": time.strftime("%Y-%m-%d"),
                  "meal": "snack", "note": "sig smoke",
                  "clientLogId": uuid.uuid4().hex},
            headers={"X-Device-Id": claimed_id, "X-Timestamp": ts,
                     "X-Nonce": nonce, "X-Signature": sig})
    check("签名冒烟: 带签名头灰度期 200", r.status_code == 200, f"http={r.status_code}")

    # 批次 4：管理封口
    r = req("PUT", f"{BASE}/app-config/ent-accept-probe",
            json={"value": "should-be-rejected"})
    check("封口: 无 token PUT app-config 403", r.status_code == 403,
          f"http={r.status_code} {r.text[:50]}")
    r = req("POST", f"{BASE}/recipes/import", json={"recipes": []})
    check("封口: 无 token POST import 403", r.status_code == 403, f"http={r.status_code}")
    r = req("DELETE", f"{BASE}/app-config/ent-accept-probe")
    check("封口: 无 token DELETE app-config 403", r.status_code == 403, f"http={r.status_code}")
    r = req("POST", f"{BASE}/recipes", json={"id": "测试/不应成功"})
    check("封口: 无 token POST recipes 403", r.status_code == 403, f"http={r.status_code}")

    # 批次 1：OCR 配额（Lv1=3/日）
    img_path = sys.argv[1] if len(sys.argv) > 1 else None
    if img_path:
        with open(img_path, "rb") as f:
            img = f.read()
        for i in range(1, 5):
            r, rj = jreq("POST", f"{BASE}/ocr/receipt",
                          params={"deviceId": claimed_id},
                          files={"file": ("receipt.png", img, "image/png")})
            if i <= 3:
                check(f"OCR: 第 {i} 次成功 usedToday={i}",
                      r.status_code == 200 and rj.get("usedToday") == i,
                      f"http={r.status_code} used={rj.get('usedToday')}")
            else:
                ok = (r.status_code == 429 and
                      rj.get("detail", {}).get("code") == "OCR_QUOTA_EXCEEDED")
                check("OCR: 第 4 次 429 QUOTA_EXCEEDED", ok,
                      f"http={r.status_code} {r.text[:70]}")
        _, rj1 = jreq("POST", f"{BASE}/ocr/receipt",
                       params={"deviceId": "accept-lines-" + uuid.uuid4().hex[:8]},
                       files={"file": ("receipt.png", img, "image/png")})
        names = [ln.get("name") for ln in (rj1.get("lines") or [])]
        check("OCR: 行解析含西红柿", any("西红柿" in n for n in names),
              f"names={names}")
        big = b"\x89PNG" + b"\x00" * (9 * 1024 * 1024)
        r = req("POST", f"{BASE}/ocr/receipt", params={"deviceId": claimed_id},
                files={"file": ("big.png", big, "image/png")})
        check("OCR: 超大图被拒(413/500)", r.status_code in (413, 500),
              f"http={r.status_code}")
        m = membership(claimed_id)
        check("OCR: 权益 ocrUsedToday=3（拒绝不计数）",
              m.get("entitlements", {}).get("ocrUsedToday") == 3,
              f"ent={m.get('entitlements')}")
    else:
        print("SKIP  OCR 配额验收（未提供测试图路径）")

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
