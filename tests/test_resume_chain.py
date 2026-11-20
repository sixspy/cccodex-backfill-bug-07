import asyncio

import pytest

from app.services import resume as resume_svc
from app.db import connect, init_db


def _pause(db, slice_no=3):
    init_db(db)
    resume_svc.pause_at(slice_no, db)


def _status_map(db):
    with connect(db) as conn:
        return {
            r["slice_no"]: r["status"]
            for r in conn.execute("SELECT slice_no, status FROM offset_ptr WHERE name='main'")
        }


def _meta(db, key):
    with connect(db) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


@pytest.mark.asyncio
async def test_resume_advances_offset_and_keeps_success_slices(tmp_path):
    db = str(tmp_path / "t.db")
    _pause(db, 3)

    before = _status_map(db)
    assert before == {1: "已成功", 2: "已成功", 3: "暂停"}
    success_before = {k: v for k, v in before.items() if v == "已成功"}

    out = resume_svc.resume(db)

    # 水线偏移跳到新片
    assert out["offset"] == 4
    assert out["from_offset"] == 3
    assert _meta(db, "offset_live") == "4"
    assert _meta(db, "offset_gate") == "4"

    after = _status_map(db)
    # 旧成功片条数与状态一致，不得被重写
    for no, st in success_before.items():
        assert after[no] == st
    assert not any(s == "重写" or s == "回滚" for s in after.values())
    # 暂停片标跳空、新片待重放，空档没被越过
    assert after[3] == "跳空"
    assert after[4] == "待重放"
    # 台账序号连续，复验无空档
    assert out["recheck"]["continuous"] is True
    assert out["recheck"]["missing"] == []


@pytest.mark.asyncio
async def test_panels_light_gap_chip_and_same_source(tmp_path):
    db = str(tmp_path / "t.db")
    _pause(db, 3)
    resume_svc.resume(db)

    panels = resume_svc.panels(db)
    # 跳空芯片在详情与策略同亮
    assert panels["detail"]["chip"] == "跳空"
    assert panels["strategy"]["chip"] == "跳空"
    assert panels["detail"]["same_source"] is True
    assert panels["water"]["same_source"] is True
    # 覆盖水线：无重写片、跳空片可点名
    assert panels["water"]["rewritten"] == []
    assert [r["slice_no"] for r in panels["water"]["skipped"]] == [3]
    assert panels["water"]["live_offset"] == 4
    # 编排留痕
    kinds = [e["event"] for e in panels["events"]]
    assert "暂停" in kinds and "续灌" in kinds
    assert panels["recheck"]["continuous"] is True


@pytest.mark.asyncio
async def test_resume_rolls_back_half_rewrite_and_legacy_dirty_state(tmp_path):
    db = str(tmp_path / "t.db")
    _pause(db, 3)
    # 模拟旧实现残留：成功片被改成“重写”、old+2 跳空占位
    with connect(db) as conn:
        conn.execute("UPDATE offset_ptr SET status='重写' WHERE slice_no=2")
        conn.execute(
            "INSERT INTO offset_ptr(name, slice_no, status) VALUES (?,?,?)",
            ("main", 5, "跳空续"),
        )

    out = resume_svc.resume(db)
    after = _status_map(db)
    # 半截重写整笔回滚为成功片
    assert after[2] == "已成功"
    assert 2 in out["rolled_back"]
    # 跳空占位被清除，新位点是暂停片的下一片而不是 old+2
    assert 5 not in after or after[5] != "跳空续"
    assert after[3] == "跳空"
    assert after[4] == "待重放"


@pytest.mark.asyncio
async def test_double_position_mismatch_blocks_old_offset(tmp_path):
    db = str(tmp_path / "t.db")
    _pause(db, 3)
    with connect(db) as conn:
        conn.execute("UPDATE meta SET value='2' WHERE key='offset_live'")

    with pytest.raises(resume_svc.ServiceError) as ei:
        resume_svc.resume(db)
    assert ei.value.status_code == 409
    assert "不一致" in ei.value.message
    # 被拦后偏移不动、台账不变
    assert _meta(db, "offset_live") == "2"
    assert _meta(db, "offset_gate") == "3"
    assert _status_map(db)[2] == "已成功"


@pytest.mark.asyncio
async def test_concurrent_resume_only_one_writes(tmp_path):
    db = str(tmp_path / "t.db")
    _pause(db, 3)

    results = await asyncio.gather(
        asyncio.to_thread(resume_svc.resume, db),
        asyncio.to_thread(resume_svc.resume, db),
        return_exceptions=True,
    )
    oks = [r for r in results if not isinstance(r, Exception)]
    errs = [r for r in results if isinstance(r, Exception)]
    assert len(oks) == 1
    assert len(errs) == 1
    assert errs[0].status_code == 409

    # 偏移行只写了一次：没有重复新片、没有双写
    with connect(db) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM offset_ptr WHERE name='main' AND slice_no=4"
        ).fetchone()["c"]
        event_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM resume_events WHERE event='续灌'"
        ).fetchone()["c"]
    assert cnt == 1
    assert event_cnt == 1


@pytest.mark.asyncio
async def test_repeat_resume_is_idempotent_guard(tmp_path):
    db = str(tmp_path / "t.db")
    _pause(db, 3)
    resume_svc.resume(db)
    with pytest.raises(resume_svc.ServiceError) as ei:
        resume_svc.resume(db)
    assert ei.value.status_code == 409
    assert "已续灌" in ei.value.message


@pytest.mark.asyncio
async def test_resume_without_pause_speaks_plain(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    with connect(db) as conn:
        conn.execute("DELETE FROM meta WHERE key='offset_live'")
        conn.execute("DELETE FROM meta WHERE key='offset_gate'")
    with pytest.raises(resume_svc.ServiceError) as ei:
        resume_svc.resume(db)
    assert ei.value.status_code == 409
    assert "暂停位点" in ei.value.message


@pytest.mark.asyncio
async def test_api_resume_chain_and_error_message(authed):
    await authed.post("/api/resume/pause", json={"slice_no": 3})
    r = await authed.post("/api/resume/go", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["chip"] == "跳空"
    assert body["offset"] == 4
    assert body["gap_slices"] == [3]

    panels = (await authed.get("/api/resume/panels")).json()
    assert panels["detail"]["chip"] == "跳空"
    assert panels["detail"]["live"] == "4"
    assert panels["detail"]["gate"] == "4"

    again = await authed.post("/api/resume/go", json={})
    assert again.status_code == 409
    assert "已续灌" in again.json()["error"]
