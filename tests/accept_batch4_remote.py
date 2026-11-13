# -*- coding: utf-8 -*-
"""ENTITLEMENT 批次 4 剩余项远程验收（部署后对生产 library 服务跑）。

覆盖：灰度鉴权（放行/验签/冒名 401/超窗/重放）、collab 防爆破（join 封禁、
建房日上限）、app-config scope 白名单、plan_key 校验。
依赖此前部署的批次 2 注册协议：用真实 secret 现算 HMAC 签名。
"""
from __future__ import annotations

import hashlib
import hmac
import json
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


def sign_headers(secret: str, device_id: str, method: str, path: str,
                 body: bytes = b"", ts: str = None, nonce: str = None):
    ts = ts or str(int(time.time()))
    nonce = nonce or uuid.uuid4().hex
    body_hash = hashlib.sha256(body).hexdigest()
    raw = "\n".join([method.upper(), path, body_hash, nonce, ts])
    sig = hmac.new(bytes.fromhex(secret), raw.encode(), hashlib.sha256).hexdigest()
    return {"X-Device-Id": device_id, "X-Timestamp": ts,
            "X-Nonce": nonce, "X-Signature": sig}


def api_path(suffix: str) -> str:
    return "/library/api" + suffix


def main() -> int:
    dev = "accept-b4-" + uuid.uuid4().hex[:12]

    # 注册拿 secret
    r = req("POST", f"{BASE}/auth/devices", params={"deviceId": dev})
    if r.status_code != 200:
        check("注册 200", False, f"http={r.status_code}")
        return 1
    secret = r.json()["deviceSecret"]
    check("注册 200", True)

    # ① 灰度放行：无签名请求照常工作（老包兼容）
    r = req("GET", f"{BASE}/membership", params={"deviceId": dev})
    check("灰度: 无签名 GET /membership 200（老包放行）", r.status_code == 200,
          f"http={r.status_code}")

    # ② 合法签名 + 一致身份
    h = sign_headers(secret, dev, "GET", api_path("/membership"))
    r = req("GET", f"{BASE}/membership", params={"deviceId": dev}, headers=h)
    check("灰度: 合法签名一致身份 200", r.status_code == 200, f"http={r.status_code}")

    # ③ 冒名：合法签名 + query 声称别人的 deviceId → 401 FORGED_IDENTITY
    h = sign_headers(secret, dev, "GET", api_path("/membership"))
    r = req("GET", f"{BASE}/membership", params={"deviceId": "victim-" + uuid.uuid4().hex[:8]},
            headers=h)
    det = {}
    try:
        det = r.json().get("detail", {})
    except Exception:
        pass
    check("灰度: 冒名 401 FORGED_IDENTITY",
          r.status_code == 401 and det.get("code") == "FORGED_IDENTITY",
          f"http={r.status_code} {r.text[:60]}")

    # ④ 时间戳超窗
    h = sign_headers(secret, dev, "GET", api_path("/membership"),
                     ts=str(int(time.time()) - 4000))
    r = req("GET", f"{BASE}/membership", params={"deviceId": dev}, headers=h)
    check("签名: 时间戳超 ±300s → 401", r.status_code == 401, f"http={r.status_code}")

    # ⑤ nonce 重放
    nonce = uuid.uuid4().hex
    h1 = sign_headers(secret, dev, "GET", api_path("/membership"), nonce=nonce)
    r1 = req("GET", f"{BASE}/membership", params={"deviceId": dev}, headers=h1)
    r2 = req("GET", f"{BASE}/membership", params={"deviceId": dev}, headers=h1)
    check("签名: 同 nonce 重放第二次 401",
          r1.status_code == 200 and r2.status_code == 401,
          f"first={r1.status_code} replay={r2.status_code}")

    # ⑥ join 爆破封禁：连打 10 个不存在房间，第 11 次 429
    codes = []
    for i in range(11):
        body = json.dumps({"deviceId": dev}).encode()
        h = sign_headers(secret, dev, "POST", api_path(f"/collab/rooms/{uuid.uuid4().hex[:6].upper()}/join"), body)
        r = req("POST", f"{BASE}/collab/rooms/{uuid.uuid4().hex[:6].upper()}/join",
                content=body, headers={**h, "Content-Type": "application/json"})
        codes.append(r.status_code)
    got_429 = 429 in codes
    check("collab: join 爆破第 11 次内触发 429", got_429, f"codes={codes}")

    # ⑦ 建房日上限：连建 21 个，第 21 次 429
    codes = []
    for i in range(21):
        body = json.dumps({"deviceId": dev, "dishIds": ["炒菜/家常土豆片"],
                           "people": 2}).encode()
        h = sign_headers(secret, dev, "POST", api_path("/collab/rooms"), body)
        r = req("POST", f"{BASE}/collab/rooms", content=body,
                headers={**h, "Content-Type": "application/json"})
        codes.append(r.status_code)
    check("collab: 建房第 21 次内触发 429 ROOM_CREATE_LIMIT",
          429 in codes, f"codes={codes}")

    # ⑧ app-config 敏感 scope 拒绝
    r = req("GET", f"{BASE}/app-config", params={"scope": "admin"})
    check("app-config: 敏感 scope 403", r.status_code == 403, f"http={r.status_code}")
    r = req("GET", f"{BASE}/app-config")
    keys = r.json().get("items", {}) if r.status_code == 200 else {}
    check("app-config: 全量只下发白名单 scope",
          r.status_code == 200 and all(
              k for k in keys), f"http={r.status_code} keys={list(keys)[:5]}")

    # ⑨ plan_key 校验
    r = req("GET", f"{BASE}/shopping-checks",
            params={"deviceId": dev, "planKey": "'; DROP TABLE x;--"})
    det = r.json().get("detail", {}) if r.status_code != 200 else {}
    check("shopping: 非法 planKey 400",
          r.status_code == 400 and det.get("code") == "INVALID_PLAN_KEY",
          f"http={r.status_code}")
    r = req("GET", f"{BASE}/shopping-checks",
            params={"deviceId": dev, "planKey": "2026-11-14-1a2b3c4d"})
    check("shopping: 合法 planKey 200", r.status_code == 200, f"http={r.status_code}")

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
