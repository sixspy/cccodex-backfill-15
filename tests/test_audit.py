from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import get_db
from app.services import windows as windows_service


@pytest.mark.asyncio
async def test_replay_writes_credential_same_op(authed):
    wid = (await authed.get("/api/windows")).json()["items"][0]["id"]
    planned = (await authed.post(f"/api/windows/{wid}/plan")).json()
    sid = planned["slices"][0]["id"]

    replayed = (await authed.post(f"/api/slices/{sid}/replay")).json()["slice"]
    assert replayed["status"] == "已落库"
    assert replayed["gate_status"] == "已出证"
    assert replayed["credential_no"].startswith("BF-")
    assert replayed["slice_key"] == f"dw_orders_d#{replayed['slice_date']}"

    detail = (await authed.get(f"/api/slices/{sid}")).json()
    assert detail["credential"]["credential_no"] == replayed["credential_no"]
    assert detail["credential"]["operator"] == "admin"
    assert detail["slice"]["gate_status"] == "已出证"
    assert any(l["action"] == "重放落库" and l["outcome"] == "成功" for l in detail["ledger"])


@pytest.mark.asyncio
async def test_gate_passes_with_credential(authed):
    wid = (await authed.get("/api/windows")).json()["items"][0]["id"]
    sid = (await authed.post(f"/api/windows/{wid}/plan")).json()["slices"][0]["id"]
    cred = (await authed.post(f"/api/slices/{sid}/replay")).json()["slice"]["credential_no"]

    r = await authed.post(f"/api/slices/{sid}/gate")
    assert r.status_code == 200
    body = r.json()
    assert body["gate"] == "放行"
    assert body["credential"]["credential_no"] == cred

    ledger = (await authed.get(f"/api/ledger?slice_id={sid}")).json()["items"]
    assert any(l["action"] == "出证闸口" and l["outcome"] == "放行" for l in ledger)


@pytest.mark.asyncio
async def test_gate_rejects_before_replay(authed):
    wid = (await authed.get("/api/windows")).json()["items"][0]["id"]
    sid = (await authed.post(f"/api/windows/{wid}/plan")).json()["slices"][0]["id"]

    r = await authed.post(f"/api/slices/{sid}/gate")
    assert r.status_code == 409
    assert "还没重放落库" in r.json()["error"]

    ledger = (await authed.get(f"/api/ledger?slice_id={sid}")).json()["items"]
    assert any(l["action"] == "出证闸口" and l["outcome"] == "拒签" for l in ledger)


@pytest.mark.asyncio
async def test_missing_credential_is_rejected_not_backfilled(authed, db_path):
    """验收：故意缺证再对账，拒签芯片须亮，目录不得把无证片算进已齐。"""
    wid = (await authed.get("/api/windows")).json()["items"][0]["id"]
    planned = (await authed.post(f"/api/windows/{wid}/plan")).json()
    for s in planned["slices"]:
        (await authed.post(f"/api/slices/{s['id']}/replay")).json()

    bad_sid = planned["slices"][1]["id"]
    bad_key = planned["slices"][1]["slice_key"]
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM audit_credentials WHERE slice_id=?", (bad_sid,))

    # 1) 闸口亮缺证拒签，且不现场补证
    r = await authed.post(f"/api/slices/{bad_sid}/gate")
    assert r.status_code == 409
    assert "查不到出证凭证" in r.json()["error"]
    with get_db(db_path) as conn:
        left = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_credentials WHERE slice_id=?", (bad_sid,)
        ).fetchone()["c"]
    assert left == 0, "闸口不得事后补证"

    # 2) 页顶拒签芯片数据源：窗口的缺证计数
    win = [w for w in (await authed.get("/api/windows")).json()["items"] if w["id"] == wid][0]
    assert win["uncertified_count"] == 1
    assert win["certified_count"] == len(planned["slices"]) - 1

    # 3) 台账亮缺证：对账结论为缺证
    report = (await authed.post("/api/coverage-reports", json={"window_id": wid})).json()["report"]
    assert report["certified"] == len(planned["slices"]) - 1
    assert report["uncertified"] == 1
    assert report["verdict"] == "缺证"

    # 4) 覆盖目录不得把无证片算进已齐
    directory = (await authed.get(f"/api/coverage-directory?window_id={wid}")).json()["items"][0]
    assert directory["complete"] is False
    assert directory["verdict"] == "缺证"
    assert directory["certified"] == len(planned["slices"]) - 1
    assert [s["slice_key"] for s in directory["uncertified_slices"]] == [bad_key]

    # 5) 目录再验拒签并留痕
    r = await authed.post("/api/coverage-directory", json={"window_id": wid})
    assert r.status_code == 409
    assert "没有出证凭证" in r.json()["error"]
    ledger = (await authed.get(f"/api/ledger?window_id={wid}")).json()["items"]
    assert any(l["action"] == "目录再验" and l["outcome"] == "拒签" for l in ledger)

    # 6) 缺证片可整片重跑补证，补证后目录转已齐
    fixed = (await authed.post(f"/api/slices/{bad_sid}/replay")).json()["slice"]
    assert fixed["gate_status"] == "已出证"
    directory = (await authed.get(f"/api/coverage-directory?window_id={wid}")).json()["items"][0]
    assert directory["complete"] is True
    assert directory["verdict"] == "已齐"


@pytest.mark.asyncio
async def test_credential_and_load_roll_back_together(authed, db_path, monkeypatch):
    """凭证与落库同成同败：写证抛错则落库一起回滚，台账另记失败。"""
    wid = (await authed.get("/api/windows")).json()["items"][0]["id"]
    sid = (await authed.post(f"/api/windows/{wid}/plan")).json()["slices"][0]["id"]

    def boom(*a, **kw):
        raise RuntimeError("凭证写失败")

    monkeypatch.setattr(windows_service, "issue_credential", boom)
    r = await authed.post(f"/api/slices/{sid}/replay")
    assert r.status_code == 500

    with get_db(db_path) as conn:
        sl = conn.execute("SELECT * FROM backfill_slices WHERE id=?", (sid,)).fetchone()
        creds = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_credentials WHERE slice_id=?", (sid,)
        ).fetchone()["c"]
    assert sl["status"] == "待重放"
    assert sl["row_count"] == 0
    assert creds == 0
    ledger = (await authed.get(f"/api/ledger?slice_id={sid}")).json()["items"]
    assert any(l["action"] == "重放落库" and l["outcome"] == "失败" for l in ledger)


@pytest.mark.asyncio
async def test_concurrent_replay_writes_single_credential(authed, db_path):
    """两名对账员抢重放：排他锁 + 一片一证，凭证行不得双写。"""
    wid = (await authed.get("/api/windows")).json()["items"][0]["id"]
    sid = (await authed.post(f"/api/windows/{wid}/plan")).json()["slices"][0]["id"]
    path = str(db_path)

    def attempt():
        try:
            windows_service.replay_slice(sid, "admin", path)
            return "ok"
        except Exception as exc:  # noqa: BLE001 - 期望其中一方拿 409
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))

    assert results.count("ok") == 1
    with get_db(db_path) as conn:
        creds = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_credentials WHERE slice_id=?", (sid,)
        ).fetchone()["c"]
    assert creds == 1


@pytest.mark.asyncio
async def test_tampered_credential_counts_as_missing(authed, db_path):
    """凭证行被改过（摘要核不上）等同缺证：不会混进已齐，也不能被闸口放行。"""
    wid = (await authed.get("/api/windows")).json()["items"][0]["id"]
    planned = (await authed.post(f"/api/windows/{wid}/plan")).json()
    for s in planned["slices"]:
        await authed.post(f"/api/slices/{s['id']}/replay")
    sid = planned["slices"][0]["id"]
    with get_db(db_path) as conn:
        conn.execute("UPDATE audit_credentials SET row_count=999999 WHERE slice_id=?", (sid,))

    r = await authed.post(f"/api/slices/{sid}/gate")
    assert r.status_code == 409
    assert "摘要" in r.json()["error"] or "行数" in r.json()["error"]

    win = [w for w in (await authed.get("/api/windows")).json()["items"] if w["id"] == wid][0]
    assert win["uncertified_count"] == 1

    directory = (await authed.get(f"/api/coverage-directory?window_id={wid}")).json()["items"][0]
    assert directory["complete"] is False
    assert directory["uncertified"] == 1

    # 重跑可整片重取新证；旧凭证行同一事务作废，不会与新证并存
    fixed = (await authed.post(f"/api/slices/{sid}/replay")).json()["slice"]
    assert fixed["gate_status"] == "已出证"
    with get_db(db_path) as conn:
        creds = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_credentials WHERE slice_id=?", (sid,)
        ).fetchone()["c"]
    assert creds == 1
    assert (await authed.get(f"/api/coverage-directory?window_id={wid}")).json()["items"][0][
        "complete"
    ] is True


@pytest.mark.asyncio
async def test_replan_blocked_after_credentials_exist(authed):
    wid = (await authed.get("/api/windows")).json()["items"][0]["id"]
    sid = (await authed.post(f"/api/windows/{wid}/plan")).json()["slices"][0]["id"]
    await authed.post(f"/api/slices/{sid}/replay")

    r = await authed.post(f"/api/windows/{wid}/plan")
    assert r.status_code == 409
    assert "审计凭证" in r.json()["error"]
