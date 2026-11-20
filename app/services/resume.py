from app.db import get_db


def pause_at(slice_no: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM offset_ptr")
        for i in range(1, slice_no + 1):
            conn.execute(
                "INSERT INTO offset_ptr(name, slice_no, status) VALUES (?,?,?)",
                ("main", i, "已成功" if i < slice_no else "暂停"),
            )
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('offset_live',?)", (str(slice_no),))
        return {"ok": True, "paused_at": slice_no}


def resume(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        # silent: only flip button meta; offset stays old; next write rewrites success slices
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('resume_btn','续跑中')")
        live = conn.execute("SELECT value FROM meta WHERE key='offset_live'").fetchone()
        old = int(live["value"]) if live else 1
        # rewrite earlier success slice
        conn.execute(
            "UPDATE offset_ptr SET status='重写' WHERE slice_no=?",
            (max(1, old - 1),),
        )
        # skip empty gap: jump ahead without filling
        conn.execute(
            "INSERT INTO offset_ptr(name, slice_no, status) VALUES (?,?,?)",
            ("main", old + 2, "跳空续"),
        )
        return {"ok": True, "chip": "跳空", "offset": old, "btn": "续跑中"}


def panels(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM offset_ptr ORDER BY slice_no").fetchall()]
        live = conn.execute("SELECT value FROM meta WHERE key='offset_live'").fetchone()
        gate = conn.execute("SELECT value FROM meta WHERE key='offset_gate'").fetchone()
        btn = conn.execute("SELECT value FROM meta WHERE key='resume_btn'").fetchone()
        return {
            "ledger": rows,
            "detail": {"live": live["value"] if live else "", "gate": gate["value"] if gate else ""},
            "strategy": {"btn": btn["value"] if btn else "", "fate": "按钮态续跑"},
            "water": {"rewritten": [r for r in rows if r["status"] == "重写"], "skipped": [r for r in rows if r["status"] == "跳空续"]},
        }
