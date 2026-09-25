"""退役/删除审计语义测试。

覆盖：
- 存在换电历史的站点/车辆不得物理删除，返回稳定 409，接口不再 500
- 退役后从默认运营列表隐藏，include_retired 显式筛选可查回，含历史名称快照
- 换电历史仍可解析出退役实体的历史名称
- 重复退役幂等，不改变历史
- 无引用的误建数据可真正删除
- 数据库约束异常回滚为一致响应，之后下一次正常请求仍可完成
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import engine
from app.main import app
from app.seed import init_db

init_db()
client = TestClient(app)


def _headers() -> dict:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _unique_plate() -> str:
    return f"沪EV{uuid.uuid4().hex[:8]}"


def _make_station_with_swap(headers: dict, station_name: str):
    """建一个站点 + 车辆 + 一条换电记录，返回 (station_id, vehicle_id)。"""
    station = client.post(
        "/api/stations",
        json={"name": station_name, "slot_total": 8, "battery_ready": 5},
        headers=headers,
    ).json()
    vehicle = client.post(
        "/api/vehicles",
        json={"plate": _unique_plate(), "model": "审计测试车", "current_soc": 20.0},
        headers=headers,
    ).json()
    swap = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": station["id"], "soc_before": 20.0, "soc_after": 100.0},
        headers=headers,
    )
    assert swap.status_code == 201, swap.text
    return station["id"], vehicle["id"]


# ---------- 删除冲突 ----------

def test_delete_referenced_station_returns_conflict_not_500():
    headers = _headers()
    station_id, _ = _make_station_with_swap(headers, "待删冲突换电站")

    resp = client.delete(f"/api/stations/{station_id}", headers=headers)
    assert resp.status_code == 409
    assert "换电历史" in resp.json()["detail"]
    # 实体仍在，可查回
    assert client.get(f"/api/stations/{station_id}", headers=headers).status_code == 200


def test_delete_referenced_vehicle_returns_conflict_not_500():
    headers = _headers()
    _, vehicle_id = _make_station_with_swap(headers, "车辆冲突换电站")

    resp = client.delete(f"/api/vehicles/{vehicle_id}", headers=headers)
    assert resp.status_code == 409
    assert "换电历史" in resp.json()["detail"]
    assert client.get(f"/api/vehicles/{vehicle_id}", headers=headers).status_code == 200


def test_delete_missing_entities_still_404():
    headers = _headers()
    assert client.delete("/api/stations/99999999", headers=headers).status_code == 404
    assert client.delete("/api/vehicles/99999999", headers=headers).status_code == 404


# ---------- 退役与审计可见性 ----------

def test_retired_station_hidden_by_default_but_queryable_with_history_name():
    headers = _headers()
    station_id, _ = _make_station_with_swap(headers, "退役前历史站名")

    # 先改名再退役：退役应快照退役时刻的名称
    renamed = client.put(f"/api/stations/{station_id}", json={"name": "退役时刻站名"}, headers=headers)
    assert renamed.status_code == 200

    retired = client.post(f"/api/stations/{station_id}/retire", headers=headers)
    assert retired.status_code == 200
    body = retired.json()
    assert body["is_retired"] is True
    assert body["retired_name"] == "退役时刻站名"
    assert body["retired_at"] is not None

    # 默认运营列表中隐藏
    default_ids = [s["id"] for s in client.get("/api/stations", headers=headers).json()]
    assert station_id not in default_ids

    # 显式筛选可查回退役实体及其历史名称
    included = client.get("/api/stations", params={"include_retired": "true"}, headers=headers).json()
    found = next(s for s in included if s["id"] == station_id)
    assert found["is_retired"] is True
    assert found["retired_name"] == "退役时刻站名"

    # 直接按 id 仍可查回
    got = client.get(f"/api/stations/{station_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["is_retired"] is True

    # 换电历史连同历史名称可查
    swaps = client.get("/api/swaps", headers=headers).json()
    record = next(r for r in swaps if r["station_id"] == station_id)
    assert record["station_name"] == "退役时刻站名"


def test_retired_vehicle_hidden_and_history_plate_preserved():
    headers = _headers()
    _, vehicle_id = _make_station_with_swap(headers, "车辆退役审计站")

    retired = client.post(f"/api/vehicles/{vehicle_id}/retire", headers=headers)
    assert retired.status_code == 200
    assert retired.json()["retired_plate"] is not None
    historical_plate = retired.json()["retired_plate"]

    default_ids = [v["id"] for v in client.get("/api/vehicles", headers=headers).json()]
    assert vehicle_id not in default_ids

    included = client.get("/api/vehicles", params={"include_retired": "true"}, headers=headers).json()
    assert any(v["id"] == vehicle_id and v["is_retired"] for v in included)

    swaps = client.get("/api/swaps", headers=headers).json()
    record = next(r for r in swaps if r["vehicle_id"] == vehicle_id)
    assert record["vehicle_plate"] == historical_plate


def test_retire_is_idempotent_and_keeps_history():
    headers = _headers()
    station_id, _ = _make_station_with_swap(headers, "重复退役站")

    first = client.post(f"/api/stations/{station_id}/retire", headers=headers).json()
    second = client.post(f"/api/stations/{station_id}/retire", headers=headers)
    assert second.status_code == 200
    again = second.json()
    # 重复退役不改变退役时间与历史名称
    assert again["retired_at"] == first["retired_at"]
    assert again["retired_name"] == first["retired_name"] == "重复退役站"


def test_retired_entities_cannot_swap_or_be_edited():
    headers = _headers()
    station_id, vehicle_id = _make_station_with_swap(headers, "停用业务站")
    client.post(f"/api/stations/{station_id}/retire", headers=headers)
    client.post(f"/api/vehicles/{vehicle_id}/retire", headers=headers)

    other_vehicle = client.post(
        "/api/vehicles", json={"plate": _unique_plate(), "current_soc": 10.0}, headers=headers
    ).json()
    # 退役站点不能再产生换电记录
    resp = client.post(
        "/api/swaps",
        json={"vehicle_id": other_vehicle["id"], "station_id": station_id, "soc_before": 10.0, "soc_after": 100.0},
        headers=headers,
    )
    assert resp.status_code == 409

    # 退役实体不可再编辑
    assert client.put(f"/api/stations/{station_id}", json={"address": "新地址"}, headers=headers).status_code == 409
    assert client.put(f"/api/vehicles/{vehicle_id}", json={"model": "新型号"}, headers=headers).status_code == 409


# ---------- 无引用误建数据可物理删除 ----------

def test_unreferenced_station_can_be_deleted():
    headers = _headers()
    sid = client.post(
        "/api/stations", json={"name": "误建空站", "slot_total": 1, "battery_ready": 0}, headers=headers
    ).json()["id"]
    assert client.delete(f"/api/stations/{sid}", headers=headers).status_code == 204
    assert client.get(f"/api/stations/{sid}", headers=headers).status_code == 404


def test_unreferenced_vehicle_can_be_deleted():
    headers = _headers()
    vid = client.post("/api/vehicles", json={"plate": _unique_plate()}, headers=headers).json()["id"]
    assert client.delete(f"/api/vehicles/{vid}", headers=headers).status_code == 204
    assert client.get(f"/api/vehicles/{vid}", headers=headers).status_code == 404


# ---------- 数据库异常回滚与会话恢复 ----------

def test_database_constraint_error_rolls_back_and_session_recovers():
    headers = _headers()
    sid = client.post(
        "/api/stations", json={"name": "触发器阻断站", "slot_total": 2, "battery_ready": 1}, headers=headers
    ).json()["id"]

    # 用触发器在数据库层强制删除失败，模拟外键约束异常
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER tmp_block_station_delete "
                "BEFORE DELETE ON stations "
                "BEGIN SELECT RAISE(ABORT, 'forced constraint failure'); END"
            )
        )
    try:
        resp = client.delete(f"/api/stations/{sid}", headers=headers)
        # 必须转换为稳定的冲突响应，而不是 500
        assert resp.status_code == 409, resp.text
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS tmp_block_station_delete"))

    # 异常已回滚：实体未被半次更新影响，且下一次正常请求仍可完成
    still_there = client.get(f"/api/stations/{sid}", headers=headers)
    assert still_there.status_code == 200
    assert still_there.json()["name"] == "触发器阻断站"

    follow_up = client.post(
        "/api/stations", json={"name": "异常后新建站", "slot_total": 3, "battery_ready": 2}, headers=headers
    )
    assert follow_up.status_code == 201, follow_up.text
    assert client.delete(f"/api/stations/{follow_up.json()['id']}", headers=headers).status_code == 204
