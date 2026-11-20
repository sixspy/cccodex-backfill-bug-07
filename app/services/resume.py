import sqlite3
import threading

from app.db import connect, get_db
from app.services.errors import ServiceError

# 状态词
ST_SUCCESS = "已成功"
ST_PAUSED = "暂停"
ST_GAP = "跳空"
ST_ROLLBACK = "回滚"
ST_PENDING = "待重放"

# 进程内续灌互斥：两侧仓管同抢续灌时，缺锁会双写偏移行
_resume_lock = threading.Lock()


def _meta_int(conn, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return int(row["value"]) if row else default


def _record_event(conn, event: str, from_offset, to_offset, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO resume_events(event, from_offset, to_offset, detail) VALUES (?,?,?,?)",
        (event, from_offset, to_offset, detail),
    )


def _recheck(conn) -> dict:
    """切片与水线复验：台账序号必须连续，不允许悄悄越过空档。"""
    nos = [
        r["slice_no"]
        for r in conn.execute(
            "SELECT DISTINCT slice_no FROM offset_ptr WHERE name='main' ORDER BY slice_no"
        ).fetchall()
    ]
    gaps = []
    if nos:
        for expect in range(nos[0], nos[-1] + 1):
            if expect not in nos:
                gaps.append(expect)
    return {
        "continuous": not gaps,
        "missing": gaps,
        "range": [nos[0], nos[-1]] if nos else [],
    }


def pause_at(slice_no: int, db_path: str | None = None) -> dict:
    if not isinstance(slice_no, int) or slice_no < 1:
        raise ServiceError("暂停片号无效", 400)
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM offset_ptr")
        for i in range(1, slice_no + 1):
            conn.execute(
                "INSERT INTO offset_ptr(name, slice_no, status) VALUES (?,?,?)",
                ("main", i, ST_SUCCESS if i < slice_no else ST_PAUSED),
            )
        # 现行偏移与可灌进度同源：暂停时双位一起落库
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('offset_live',?)",
            (str(slice_no),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('offset_gate',?)",
            (str(slice_no),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('resume_btn',?)",
            ("续跑",),
        )
        _record_event(conn, "暂停", slice_no, slice_no, f"暂停于片{slice_no}")
        recheck = _recheck(conn)
        return {"ok": True, "paused_at": slice_no, "recheck": recheck}


def resume(db_path: str | None = None) -> dict:
    # 抢锁失败即白话拒绝，杜绝两侧仓管并发双写偏移行
    if not _resume_lock.acquire(blocking=False):
        raise ServiceError("已有续灌在进行中，请稍后再试", 409)
    try:
        conn = connect(db_path)
        try:
            # 立刻拿库级写锁：跨进程/跨连接的并发续灌同样被挡
            conn.execute("BEGIN IMMEDIATE")

            live = _meta_int(conn, "offset_live", 0)
            gate = _meta_int(conn, "offset_gate", 0)
            if live < 1:
                raise ServiceError("没有可续灌的暂停位点，请先暂停登记", 409)
            # 双位即拦：现行位点与可灌进度不同源，拒绝旧偏移灌入
            if live != gate:
                raise ServiceError(
                    f"现行偏移({live})与可灌进度({gate})不一致，已拦住旧偏移灌入",
                    409,
                )

            # 已续过的幂等拦截：按钮态已切换且位点已前移，不许再灌一次
            btn_row = conn.execute(
                "SELECT value FROM meta WHERE key='resume_btn'"
            ).fetchone()
            if btn_row and btn_row["value"] == "续跑中":
                raise ServiceError("该暂停位点已续灌，请勿重复续灌", 409)

            old = live

            # 工程命运①：打开瞬间先整笔回滚半截重写，恢复旧成功片
            rewritten = [
                r["slice_no"]
                for r in conn.execute(
                    "SELECT slice_no FROM offset_ptr WHERE name='main' AND status=?",
                    (ST_ROLLBACK,),
                ).fetchall()
            ]
            conn.execute(
                "UPDATE offset_ptr SET status=? WHERE name='main' AND status=?",
                (ST_SUCCESS, ST_ROLLBACK),
            )

            # 自愈历史脏数据：旧实现留下的“重写”同样回滚为成功片
            legacy_rewritten = [
                r["slice_no"]
                for r in conn.execute(
                    "SELECT slice_no FROM offset_ptr WHERE name='main' AND status='重写'"
                ).fetchall()
            ]
            conn.execute(
                "UPDATE offset_ptr SET status=? WHERE name='main' AND status='重写'",
                (ST_SUCCESS,),
            )

            # 自愈历史脏数据：旧实现插入的“跳空续”占位行先清掉，后续按序补位
            stale_skip = [
                r["slice_no"]
                for r in conn.execute(
                    "SELECT slice_no FROM offset_ptr WHERE name='main' AND status='跳空续'"
                ).fetchall()
            ]
            conn.execute(
                "DELETE FROM offset_ptr WHERE name='main' AND status='跳空续'"
            )

            # 旧成功片一律不许重写：旧位点之前若缺行，按成功事实补回
            existing = {
                r["slice_no"]
                for r in conn.execute(
                    "SELECT slice_no FROM offset_ptr WHERE name='main'"
                ).fetchall()
            }
            restored = []
            for no in range(1, old):
                if no not in existing:
                    conn.execute(
                        "INSERT INTO offset_ptr(name, slice_no, status) VALUES (?,?,?)",
                        ("main", no, ST_SUCCESS),
                    )
                    restored.append(no)

            # 空档不得越过：暂停片标跳空、留待补灌，而不是直接 old+2 跳走
            gap_row = conn.execute(
                "SELECT status FROM offset_ptr WHERE name='main' AND slice_no=?",
                (old,),
            ).fetchone()
            if gap_row is None:
                conn.execute(
                    "INSERT INTO offset_ptr(name, slice_no, status) VALUES (?,?,?)",
                    ("main", old, ST_GAP),
                )
            else:
                conn.execute(
                    "UPDATE offset_ptr SET status=? WHERE name='main' AND slice_no=?",
                    (ST_GAP, old),
                )

            # 工程命运②：旁路位点凭证，原子切换到新偏移后再挡住旧偏移
            new_offset = old + 1
            conn.execute(
                "INSERT OR REPLACE INTO offset_ptr(name, slice_no, status) VALUES (?,?,?)",
                ("main", new_offset, ST_PENDING),
            )
            # 双位同源写库：不再只改按钮态
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES ('offset_live',?)",
                (str(new_offset)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES ('offset_gate',?)",
                (str(new_offset)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES ('resume_btn',?)",
                ("续跑中",),
            )

            # 编排留痕
            detail_parts = [f"回滚重写片{sorted(set(rewritten + legacy_rewritten))}"]
            if restored:
                detail_parts.append(f"补回成功片{restored}")
            if stale_skip:
                detail_parts.append(f"清除跳空占位{stale_skip}")
            detail_parts.append(f"片{old}标跳空待补，新位点片{new_offset}")
            _record_event(conn, "续灌", old, new_offset, "；".join(detail_parts))

            recheck = _recheck(conn)
            if not recheck["continuous"]:
                # 复验不通过则整笔回滚，绝不带跳空收口
                raise ServiceError(
                    f"切片复验发现空档{recheck['missing']}，已整笔回滚本次续灌",
                    409,
                )

            conn.commit()
            return {
                "ok": True,
                "chip": ST_GAP,
                "gap_slices": [old],
                "offset": new_offset,
                "from_offset": old,
                "btn": "续跑中",
                "rolled_back": sorted(set(rewritten + legacy_rewritten)),
                "recheck": recheck,
            }
        except sqlite3.OperationalError as exc:
            conn.rollback()
            # database is locked 等写锁冲突，吐白话而不是堆栈
            raise ServiceError(f"续灌抢锁失败：{exc}，请稍后重试", 409) from exc
        except ServiceError:
            # 白话业务错误（双位不一致、复验失败等）：整笔回滚后原样上抛
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    finally:
        _resume_lock.release()


def panels(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM offset_ptr ORDER BY slice_no"
            ).fetchall()
        ]
        live = conn.execute("SELECT value FROM meta WHERE key='offset_live'").fetchone()
        gate = conn.execute("SELECT value FROM meta WHERE key='offset_gate'").fetchone()
        btn = conn.execute("SELECT value FROM meta WHERE key='resume_btn'").fetchone()
        events = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM resume_events ORDER BY id DESC LIMIT 10"
            ).fetchall()
        ]
        recheck = _recheck(conn)
        live_v = int(live["value"]) if live else 0
        gate_v = int(gate["value"]) if gate else 0
        gap_rows = [r for r in rows if r["status"] == ST_GAP]
        same_source = live_v == gate_v and live_v > 0
        return {
            "ledger": rows,
            "detail": {
                "live": live["value"] if live else "",
                "gate": gate["value"] if gate else "",
                "same_source": same_source,
                "chip": ST_GAP if gap_rows else "",
            },
            "strategy": {
                "btn": btn["value"] if btn else "",
                "fate": "回滚半截重写后原子切换新偏移",
                "chip": ST_GAP if gap_rows else "",
            },
            "water": {
                "rewritten": [r for r in rows if r["status"] == ST_ROLLBACK],
                "skipped": [r for r in rows if r["status"] == ST_GAP],
                "live_offset": live_v,
                "gate_offset": gate_v,
                "same_source": same_source,
                "recheck": recheck,
            },
            "events": events,
            "recheck": recheck,
        }
