"""审计凭证、出证闸口、审计台账、覆盖目录。

出证命运（全局唯一）：**同步同事务**。
重放落库与写审计凭证落在同一个 BEGIN IMMEDIATE ... COMMIT 里，
同成同败；缺证视为异常数据，闸口一律拒签、不事后补证，
唯一纠偏路径是对缺证切片重跑（重跑本身也必须在同一事务里重新取证）。
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from app.db import get_db
from app.services.errors import ServiceError

GATE_OK = "已出证"
GATE_MISSING = "缺证"
GATE_PENDING = "未出证"


def make_slice_key(table_name: str, slice_date: str) -> str:
    return f"{table_name}#{slice_date}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(payload: dict) -> str:
    canon = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _credential_payload(credential_no, window_id, slice_id, slice_key, operator, row_count, issued_at):
    return {
        "credential_no": credential_no,
        "window_id": window_id,
        "slice_id": slice_id,
        "slice_key": slice_key,
        "operator": operator,
        "row_count": row_count,
        "issued_at": issued_at,
    }


def credential_no_for(
    conn: sqlite3.Connection, window_id: int, slice_date: str, slice_id: int
) -> str:
    """凭证号可读可核：BF-窗口-日期-切片号-放次。

    放次取台账里该片既往成功落库/重跑的笔数 +1（台账不随凭证删除而消失），
    同片重跑补证会得到新的放次，凭证号不会复用。
    """
    prior = conn.execute(
        "SELECT COUNT(*) AS c FROM audit_ledger WHERE slice_id=? AND outcome='成功' "
        "AND action IN ('重放落库','切片重跑')",
        (slice_id,),
    ).fetchone()["c"]
    return f"BF-{window_id:02d}-{slice_date.replace('-', '')}-{slice_id:05d}-{prior + 1:02d}"


def issue_credential(conn: sqlite3.Connection, slice: sqlite3.Row, operator: str, row_count: int) -> dict:
    """在调用方已开启的事务里写凭证；不自行提交。"""
    issued_at = _now()
    no = credential_no_for(conn, slice["window_id"], slice["slice_date"], slice["id"])
    key = slice["slice_key"] or make_slice_key(slice["table_name"], slice["slice_date"])
    payload = _credential_payload(no, slice["window_id"], slice["id"], key, operator, row_count, issued_at)
    conn.execute(
        "INSERT INTO audit_credentials"
        "(credential_no, window_id, slice_id, slice_key, operator, row_count, digest, issued_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (no, slice["window_id"], slice["id"], key, operator, row_count, _digest(payload), issued_at),
    )
    row = conn.execute(
        "SELECT * FROM audit_credentials WHERE slice_id=?", (slice["id"],)
    ).fetchone()
    return dict(row)


def get_credential(conn: sqlite3.Connection, slice_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM audit_credentials WHERE slice_id=?", (slice_id,)
    ).fetchone()
    return dict(row) if row else None


def check_credential(cred: dict) -> tuple[bool, str]:
    """复核凭证摘要，防止凭证号对不上落库行。"""
    payload = _credential_payload(
        cred["credential_no"],
        cred["window_id"],
        cred["slice_id"],
        cred["slice_key"],
        cred["operator"],
        cred["row_count"],
        cred["issued_at"],
    )
    if _digest(payload) != cred["digest"]:
        return False, "凭证摘要与落库记录对不上，按缺证处理"
    return True, ""


def credential_problem(conn: sqlite3.Connection, cred: dict) -> str:
    """凭证要同时核两件事：自身摘要没被动过，且与落库行对得上。"""
    ok, reason = check_credential(cred)
    if not ok:
        return reason
    sl = conn.execute(
        "SELECT slice_key, row_count, status FROM backfill_slices WHERE id=?",
        (cred["slice_id"],),
    ).fetchone()
    if not sl:
        return "凭证指向的切片不存在"
    if sl["status"] != "已落库":
        return "凭证指向的切片不在已落库状态"
    if sl["slice_key"] != cred["slice_key"]:
        return "凭证片键与落库片键不一致"
    if sl["row_count"] != cred["row_count"]:
        return "凭证行数与落库行数不一致"
    return ""


def valid_credentials(
    conn: sqlite3.Connection, window_id: int | None = None
) -> dict[int, dict]:
    """slice_id → 凭证，仅保留核得上的。

    「有证」全工程只此一个定义：台账、片键详情、拒签芯片、覆盖目录都从这里取，
    避免某一处把摘要对不上、或与落库行对不上的凭证也当已出证。
    """
    sql = "SELECT * FROM audit_credentials"
    args: list = []
    if window_id is not None:
        sql += " WHERE window_id=?"
        args.append(window_id)
    out: dict[int, dict] = {}
    for row in conn.execute(sql, args).fetchall():
        cred = dict(row)
        if not credential_problem(conn, cred):
            out[cred["slice_id"]] = cred
    return out


def record_ledger(
    conn: sqlite3.Connection,
    *,
    action: str,
    outcome: str,
    operator: str,
    detail: str,
    window_id: int | None = None,
    slice_id: int | None = None,
    credential_no: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_ledger(window_id, slice_id, action, outcome, credential_no, operator, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (window_id, slice_id, action, outcome, credential_no, operator, detail),
    )


def record_ledger_standalone(
    db_path: str | None,
    *,
    action: str,
    outcome: str,
    operator: str,
    detail: str,
    window_id: int | None = None,
    slice_id: int | None = None,
    credential_no: str | None = None,
) -> None:
    """失败留痕独立落一笔：主事务已回滚，台账不能被一起回滚掉。"""
    try:
        with get_db(db_path, immediate=True) as conn:
            record_ledger(
                conn,
                action=action,
                outcome=outcome,
                operator=operator,
                detail=detail,
                window_id=window_id,
                slice_id=slice_id,
                credential_no=credential_no,
            )
    except Exception:
        # 留痕失败不能掩盖原始业务错误
        pass


def slice_context(conn: sqlite3.Connection, slice_id: int) -> dict | None:
    row = conn.execute(
        "SELECT s.*, w.table_name, w.start_date, w.end_date, w.status AS window_status "
        "FROM backfill_slices s JOIN backfill_windows w ON w.id = s.window_id "
        "WHERE s.id=?",
        (slice_id,),
    ).fetchone()
    return dict(row) if row else None


def open_gate(slice_id: int, operator: str, db_path: str | None = None) -> dict:
    """出证闸口：只认当场同事务写下的凭证；缺证一律拒签，不现场补证。"""
    try:
        with get_db(db_path, immediate=True) as conn:
            sl = slice_context(conn, slice_id)
            if not sl:
                raise ServiceError("切片不存在", 404)
            if sl["status"] != "已落库":
                raise ServiceError(
                    f"片 {sl['slice_key']} 还没重放落库，闸口没有凭证可亮", 409
                )
            cred = get_credential(conn, slice_id)
            if not cred:
                raise ServiceError(
                    f"片 {sl['slice_key']} 已落库但查不到出证凭证，闸口拒签；请重跑该片重新落库取证", 409
                )
            problem = credential_problem(conn, cred)
            if problem:
                raise ServiceError(
                    f"片 {sl['slice_key']} 的凭证 {cred['credential_no']} {problem}，闸口拒签", 409
                )
            record_ledger(
                conn,
                action="出证闸口",
                outcome="放行",
                operator=operator,
                detail=f"亮证 {cred['credential_no']}，片键 {sl['slice_key']}，落库 {cred['row_count']} 行",
                window_id=sl["window_id"],
                slice_id=slice_id,
                credential_no=cred["credential_no"],
            )
            return {"gate": "放行", "slice": sl, "credential": cred}
    except ServiceError as exc:
        ctx = _safe_slice_ctx(db_path, slice_id)
        record_ledger_standalone(
            db_path,
            action="出证闸口",
            outcome="拒签",
            operator=operator,
            detail=exc.message,
            window_id=ctx["window_id"] if ctx else None,
            slice_id=slice_id,
            credential_no=ctx.get("credential_no") if ctx else None,
        )
        raise


def _safe_slice_ctx(db_path: str | None, slice_id: int) -> dict | None:
    try:
        with get_db(db_path) as conn:
            row = conn.execute(
                "SELECT s.window_id, (SELECT credential_no FROM audit_credentials WHERE slice_id=s.id) "
                "AS credential_no FROM backfill_slices s WHERE s.id=?",
                (slice_id,),
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def slice_detail(slice_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        sl = conn.execute(
            "SELECT s.*, w.table_name, w.start_date, w.end_date, w.status AS window_status "
            "FROM backfill_slices s JOIN backfill_windows w ON w.id=s.window_id WHERE s.id=?",
            (slice_id,),
        ).fetchone()
        if not sl:
            raise ServiceError("切片不存在", 404)
        cred = get_credential(conn, slice_id)
        problem = ""
        if cred:
            problem = credential_problem(conn, cred)
            if problem:
                cred = dict(cred, invalid_reason=problem)
        trail = conn.execute(
            "SELECT * FROM audit_ledger WHERE slice_id=? ORDER BY id DESC LIMIT 50",
            (slice_id,),
        ).fetchall()
        data = dict(sl)
        valid = bool(cred) and not problem
        data["gate_status"] = (
            GATE_OK
            if valid
            else (GATE_PENDING if data["status"] != "已落库" else GATE_MISSING)
        )
        data["credential_no"] = cred["credential_no"] if valid else None
        return {
            "slice": data,
            "credential": cred,
            "ledger": [dict(r) for r in trail],
        }


def list_ledger(
    window_id: int | None = None, slice_id: int | None = None, db_path: str | None = None
) -> list[dict]:
    sql = (
        "SELECT l.*, w.table_name FROM audit_ledger l "
        "LEFT JOIN backfill_windows w ON w.id=l.window_id WHERE 1=1"
    )
    args: list = []
    if window_id is not None:
        sql += " AND l.window_id=?"
        args.append(window_id)
    if slice_id is not None:
        sql += " AND l.slice_id=?"
        args.append(slice_id)
    sql += " ORDER BY l.id DESC LIMIT 300"
    with get_db(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def coverage_directory(window_id: int | None = None, db_path: str | None = None) -> list[dict]:
    """覆盖目录：只把「持有效凭证的落库片」算进已齐，无证片单列缺口。"""
    win_sql = "SELECT w.* FROM backfill_windows w"
    args: list = []
    if window_id is not None:
        win_sql += " WHERE w.id=?"
        args.append(window_id)
    win_sql += " ORDER BY w.id"
    with get_db(db_path) as conn:
        wins = conn.execute(win_sql, args).fetchall()
        certified = valid_credentials(conn)
        out = []
        for win in wins:
            slices = conn.execute(
                "SELECT s.id, s.slice_date, s.slice_key, s.row_count, s.status "
                "FROM backfill_slices s WHERE s.window_id=? ORDER BY s.slice_date",
                (win["id"],),
            ).fetchall()
            loaded = [s for s in slices if s["status"] == "已落库"]
            cert = [s for s in loaded if s["id"] in certified]
            uncert = [
                {
                    "id": s["id"],
                    "window_id": win["id"],
                    "slice_date": s["slice_date"],
                    "slice_key": s["slice_key"],
                    "row_count": s["row_count"],
                }
                for s in loaded
                if s["id"] not in certified
            ]
            r = {
                "window_id": win["id"],
                "table_name": win["table_name"],
                "start_date": win["start_date"],
                "end_date": win["end_date"],
                "status": win["status"],
                "planned": len(slices),
                "loaded": len(loaded),
                "certified": len(cert),
                "uncertified": len(uncert),
                "missing": len(slices) - len(loaded),
                "uncertified_slices": uncert,
            }
            # 已齐 = 有切片、全部落库、且全部持有效凭证；无证片不算已齐
            r["complete"] = (
                r["planned"] > 0
                and r["loaded"] == r["planned"]
                and r["certified"] == r["planned"]
            )
            if r["planned"] == 0:
                r["verdict"] = "未计划"
            elif r["complete"]:
                r["verdict"] = "已齐"
            elif r["uncertified"] > 0:
                r["verdict"] = "缺证"
            else:
                r["verdict"] = "未齐"
            out.append(r)
    return out


def verify_directory(window_id: int, operator: str, db_path: str | None = None) -> dict:
    """目录再验：无证片不放行，且留痕。

    先在读事务里核目录，再独立落一笔留痕——拒签时主事务要回滚，
    留痕若写在里面会被一起回滚掉。
    """
    with get_db(db_path) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        slices = conn.execute(
            "SELECT id, slice_date, slice_key, row_count, status FROM backfill_slices "
            "WHERE window_id=? ORDER BY slice_date",
            (window_id,),
        ).fetchall()
        certified = valid_credentials(conn, window_id)

    loaded = [s for s in slices if s["status"] == "已落库"]
    uncert = [s for s in loaded if s["id"] not in certified]
    planned, loaded_n = len(slices), len(loaded)

    def reject(msg: str):
        record_ledger_standalone(
            db_path, action="目录再验", outcome="拒签", operator=operator,
            detail=msg, window_id=window_id,
        )
        raise ServiceError(msg, 409)

    if planned == 0:
        reject("该窗口还没有切片计划，目录无片可验")
    if uncert:
        names = "、".join(s["slice_date"] for s in uncert[:5])
        more = "" if len(uncert) <= 5 else f" 等 {len(uncert)} 片"
        reject(
            f"窗口#{window_id} {win['table_name']} 有 {len(uncert)} 片落库但没有出证凭证"
            f"（{names}{more}），目录未齐，不予放行"
        )
    if loaded_n != planned:
        reject(
            f"窗口#{window_id} {win['table_name']} 计划{planned}片，"
            f"已落库{loaded_n}片，仍缺{planned - loaded_n}片，目录未齐"
        )
    msg = (
        f"窗口#{window_id} {win['table_name']} 计划{planned}片全部落库且凭证齐全，"
        f"目录已齐可放行"
    )
    record_ledger_standalone(
        db_path, action="目录再验", outcome="放行", operator=operator,
        detail=msg, window_id=window_id,
    )
    return {
        "verdict": "已齐",
        "detail": msg,
        "directory": coverage_directory(window_id, db_path)[0],
    }
