from datetime import date, timedelta

from app.db import get_db
from app.services.audit import (
    GATE_MISSING,
    GATE_OK,
    GATE_PENDING,
    credential_problem,
    get_credential,
    issue_credential,
    make_slice_key,
    record_ledger,
    record_ledger_standalone,
    valid_credentials,
)
from app.services.errors import ServiceError


def list_windows(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT w.*, "
            "(SELECT COUNT(*) FROM backfill_slices s WHERE s.window_id=w.id) AS slice_count, "
            "(SELECT COUNT(*) FROM backfill_slices s WHERE s.window_id=w.id AND s.status='已落库') AS loaded_count "
            "FROM backfill_windows w ORDER BY w.id"
        ).fetchall()
        # 「有证」只认摘要核得上的凭证，与闸口/目录同一口径
        certified = valid_credentials(conn)
        out = []
        for row in rows:
            w = dict(row)
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM backfill_slices WHERE window_id=? AND status='已落库'",
                    (w["id"],),
                ).fetchall()
            ]
            w["certified_count"] = sum(1 for sid in ids if sid in certified)
            w["uncertified_count"] = len(ids) - w["certified_count"]
            out.append(w)
        return out


def _daterange(start: str, end: str) -> list[str]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if e < s:
        raise ServiceError("结束日不能早于开始日")
    out = []
    cur = s
    while cur <= e:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def plan_slices(
    window_id: int, operator: str = "system", db_path: str | None = None
) -> dict:
    with get_db(db_path, immediate=True) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        certified = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_credentials WHERE window_id=?", (window_id,)
        ).fetchone()["c"]
        if certified:
            raise ServiceError(
                f"窗口已有 {certified} 张审计凭证，重新计划会抹掉落库与出证记录，拒绝执行", 409
            )
        loaded = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=? AND status='已落库'",
            (window_id,),
        ).fetchone()["c"]
        if loaded:
            raise ServiceError(
                f"窗口已有 {loaded} 片落库（含旧版无证数据），重新计划会抹掉落库记录，拒绝执行", 409
            )
        if win["status"] not in ("草稿", "已计划"):
            raise ServiceError("当前状态不可再生成切片", 409)
        days = _daterange(win["start_date"], win["end_date"])
        conn.execute("DELETE FROM backfill_slices WHERE window_id=?", (window_id,))
        for d in days:
            conn.execute(
                "INSERT INTO backfill_slices(window_id, slice_date, slice_key, status, row_count) "
                "VALUES (?,?,?, '待重放', 0)",
                (window_id, d, make_slice_key(win["table_name"], d)),
            )
        conn.execute(
            "UPDATE backfill_windows SET status='已计划' WHERE id=?",
            (window_id,),
        )
        record_ledger(
            conn,
            action="切片计划",
            outcome="成功",
            operator=operator,
            detail=f"窗口#{window_id} {win['table_name']} 生成 {len(days)} 片",
            window_id=window_id,
        )
        slices = conn.execute(
            "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date",
            (window_id,),
        ).fetchall()
        return {"window_id": window_id, "slices": [dict(s) for s in slices]}


def list_slices(window_id: int, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT s.* FROM backfill_slices s WHERE s.window_id=? ORDER BY s.slice_date",
            (window_id,),
        ).fetchall()
        certified = valid_credentials(conn, window_id)
        out = []
        for row in rows:
            s = dict(row)
            cred = certified.get(s["id"])
            s["credential_no"] = cred["credential_no"] if cred else None
            s["credential_operator"] = cred["operator"] if cred else None
            if s["status"] != "已落库":
                s["gate_status"] = GATE_PENDING
            elif cred:
                s["gate_status"] = GATE_OK
            else:
                s["gate_status"] = GATE_MISSING
            out.append(s)
        return out


def replay_slice(
    slice_id: int, operator: str = "system", db_path: str | None = None
) -> dict:
    """重放落库 + 当场写审计凭证，同一事务同成同败。

    出证命运：同步同事务。凭证插入失败则落库一起回滚；
    已落库但缺证的片只能整片重跑补证，闸口不认事后补证。
    """
    rerun = False
    try:
        with get_db(db_path, immediate=True) as conn:
            sl = conn.execute(
                "SELECT s.*, w.table_name FROM backfill_slices s "
                "JOIN backfill_windows w ON w.id=s.window_id WHERE s.id=?",
                (slice_id,),
            ).fetchone()
            if not sl:
                raise ServiceError("切片不存在", 404)

            cred = get_credential(conn, slice_id)
            if cred and credential_problem(conn, cred):
                # 核不上等同缺证，走整片重跑补证
                cred = None
            if sl["status"] == "已落库" and cred:
                raise ServiceError(
                    f"切片已落库并已出证 {cred['credential_no']}，不重复重放", 409
                )
            if sl["status"] == "已落库" and not cred:
                # 缺证纠偏唯一路径：整片重跑，重跑与新凭证同一事务。
                # 摘要核不上的旧凭证行在同一事务里先作废，再写新证。
                conn.execute("DELETE FROM audit_credentials WHERE slice_id=?", (slice_id,))
                rerun = True
            elif sl["status"] not in ("待重放", "失败"):
                raise ServiceError("仅待重放或失败切片可重放", 409)

            rows = 100 + int(sl["slice_date"].replace("-", "")) % 97
            slice_key = sl["slice_key"] or make_slice_key(sl["table_name"], sl["slice_date"])
            conn.execute(
                "UPDATE backfill_slices SET status='已落库', row_count=?, slice_key=? WHERE id=?",
                (rows, slice_key, slice_id),
            )
            # 与落库同一事务写证；任何一步失败整笔回滚
            credential = issue_credential(
                conn, dict(sl, slice_key=slice_key), operator, rows
            )
            conn.execute(
                "UPDATE backfill_windows SET status='执行中' WHERE id=?",
                (sl["window_id"],),
            )
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=? AND status!='已落库'",
                (sl["window_id"],),
            ).fetchone()["c"]
            if pending == 0:
                conn.execute(
                    "UPDATE backfill_windows SET status='已完成' WHERE id=?",
                    (sl["window_id"],),
                )
            action = "切片重跑" if rerun else "重放落库"
            record_ledger(
                conn,
                action=action,
                outcome="成功",
                operator=operator,
                detail=(
                    f"片 {slice_key} 落库 {rows} 行，当场出证 {credential['credential_no']}"
                    + ("（缺证纠偏重跑）" if rerun else "")
                ),
                window_id=sl["window_id"],
                slice_id=slice_id,
                credential_no=credential["credential_no"],
            )
            row = conn.execute(
                "SELECT s.*, ? AS gate_status FROM backfill_slices s WHERE s.id=?",
                (GATE_OK, slice_id),
            ).fetchone()
            out = dict(row)
            out["credential_no"] = credential["credential_no"]
            return out
    except ServiceError as exc:
        record_ledger_standalone(
            db_path,
            action="切片重跑" if rerun else "重放落库",
            outcome="失败",
            operator=operator,
            detail=exc.message,
            slice_id=slice_id,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - 未预期错误同样留痕后上抛
        record_ledger_standalone(
            db_path,
            action="切片重跑" if rerun else "重放落库",
            outcome="失败",
            operator=operator,
            detail=f"落库/出证事务已整体回滚：{type(exc).__name__}: {exc}",
            slice_id=slice_id,
        )
        raise


def coverage_report(
    window_id: int, operator: str = "system", db_path: str | None = None
) -> dict:
    with get_db(db_path, immediate=True) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        planned = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=?",
            (window_id,),
        ).fetchone()["c"]
        loaded = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=? AND status='已落库'",
            (window_id,),
        ).fetchone()["c"]
        certified = len(valid_credentials(conn, window_id))
        uncertified = loaded - certified
        missing = planned - loaded
        if uncertified:
            verdict = "缺证"
        elif planned and missing == 0:
            verdict = "已齐"
        else:
            verdict = "未齐"
        summary = (
            f"窗口#{window_id} {win['table_name']}：计划{planned}片，已落库{loaded}，"
            f"有证{certified}，缺证{uncertified}，未落库{missing}；对账结论 {verdict}"
        )
        cur = conn.execute(
            "INSERT INTO coverage_reports"
            "(window_id, planned, loaded, certified, uncertified, missing, summary) "
            "VALUES (?,?,?,?,?,?,?)",
            (window_id, planned, loaded, certified, uncertified, missing, summary),
        )
        record_ledger(
            conn,
            action="覆盖对账",
            outcome=verdict,
            operator=operator,
            detail=summary,
            window_id=window_id,
        )
        row = conn.execute(
            "SELECT * FROM coverage_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        out = dict(row)
        out["verdict"] = verdict
        return out


def list_coverage(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM coverage_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
