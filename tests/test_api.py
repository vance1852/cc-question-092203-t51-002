"""接口冒烟测试：覆盖认证、鉴权、CRUD、换电与统计，并校验中文编码。"""
import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.seed import init_db

init_db()
client = TestClient(app)


def _login() -> str:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_login()}"}


def test_health():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_login_wrong_password():
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    assert resp.status_code == 401


def test_requires_auth():
    # 未带 token 访问受保护资源应被拦截
    resp = client.get("/api/stations")
    assert resp.status_code == 401


def test_me_and_chinese_encoding():
    resp = client.get("/api/auth/me", headers=_auth_headers())
    assert resp.status_code == 200
    # 中文显示名必须正确返回，验证 UTF-8 编码无乱码
    assert resp.json()["display_name"] == "平台管理员"


def test_seed_stations_present_with_chinese():
    resp = client.get("/api/stations", headers=_auth_headers())
    assert resp.status_code == 200
    stations = resp.json()
    assert len(stations) >= 4
    assert any("换电站" in s["name"] for s in stations)


def test_station_crud_and_validation():
    headers = _auth_headers()
    # 非法数据：满电电池数 > 仓位总数
    bad = client.post("/api/stations", json={"name": "测试站", "slot_total": 2, "battery_ready": 5}, headers=headers)
    assert bad.status_code == 422

    created = client.post(
        "/api/stations",
        json={"name": "西站测试换电站", "address": "测试路 1 号", "slot_total": 10, "battery_ready": 6},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert created.json()["name"] == "西站测试换电站"

    updated = client.put(f"/api/stations/{sid}", json={"status": "maintenance"}, headers=headers)
    assert updated.status_code == 200
    assert updated.json()["status"] == "maintenance"

    deleted = client.delete(f"/api/stations/{sid}", headers=headers)
    assert deleted.status_code == 204
    assert client.get(f"/api/stations/{sid}", headers=headers).status_code == 404


def test_vehicle_unique_plate():
    headers = _auth_headers()
    plate = f"测{uuid.uuid4().hex[:6]}"
    first = client.post("/api/vehicles", json={"plate": plate, "model": "测试车型"}, headers=headers)
    assert first.status_code == 201, first.text
    dup = client.post("/api/vehicles", json={"plate": plate}, headers=headers)
    assert dup.status_code == 409


def test_swap_flow_updates_state():
    headers = _auth_headers()
    # 取一个有满电电池的运营站
    stations = client.get("/api/stations", headers=headers).json()
    station = next(s for s in stations if s["battery_ready"] > 0)
    plate = f"沪EV{uuid.uuid4().hex[:4]}"
    vehicle = client.post(
        "/api/vehicles", json={"plate": plate, "model": "换电测试车", "current_soc": 10.0}, headers=headers
    ).json()

    before_ready = station["battery_ready"]
    swap = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": station["id"], "soc_before": 10.0, "soc_after": 100.0},
        headers=headers,
    )
    assert swap.status_code == 201, swap.text
    assert swap.json()["station_name"] == station["name"]

    # 车辆电量应更新、站点可用电池应减一
    v_after = client.get(f"/api/vehicles/{vehicle['id']}", headers=headers).json()
    assert v_after["current_soc"] == 100.0
    s_after = client.get(f"/api/stations/{station['id']}", headers=headers).json()
    assert s_after["battery_ready"] == before_ready - 1


def test_swap_invalid_soc():
    headers = _auth_headers()
    stations = client.get("/api/stations", headers=headers).json()
    station = next(s for s in stations if s["battery_ready"] > 0)
    vehicles = client.get("/api/vehicles", headers=headers).json()
    bad = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicles[0]["id"], "station_id": station["id"], "soc_before": 90.0, "soc_after": 50.0},
        headers=headers,
    )
    assert bad.status_code == 422


def test_dashboard_stats():
    resp = client.get("/api/dashboard/stats", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["station_total"] >= 4
    assert data["vehicle_total"] >= 5
    assert "battery_ready_total" in data


# ---------- 退役/删除审计语义 ----------
def _make_station_with_swap(headers: dict, plate_suffix: str):
    """造一个有满电电池的站点、一辆车并登记一次换电，返回 (sid, vid)。"""
    station = client.post(
        "/api/stations",
        json={"name": f"退役审计站{plate_suffix}", "slot_total": 5, "battery_ready": 2},
        headers=headers,
    ).json()
    vehicle = client.post(
        "/api/vehicles",
        json={"plate": f"沪EV{plate_suffix}", "model": "审计测试车", "current_soc": 20.0},
        headers=headers,
    ).json()
    swap = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": station["id"], "soc_before": 20.0, "soc_after": 100.0},
        headers=headers,
    )
    assert swap.status_code == 201, swap.text
    return station["id"], vehicle["id"]


def test_delete_referenced_station_returns_conflict():
    headers = _auth_headers()
    sid, _ = _make_station_with_swap(headers, uuid.uuid4().hex[:6])
    resp = client.delete(f"/api/stations/{sid}", headers=headers)
    # 存在换电历史：不得物理删除，返回稳定的 409 冲突而非 500
    assert resp.status_code == 409, resp.text
    assert "换电历史" in resp.json()["detail"]
    # 实体与历史记录仍在
    assert client.get(f"/api/stations/{sid}", headers=headers).status_code == 200
    swaps = client.get("/api/swaps", headers=headers).json()
    assert any(s["station_id"] == sid for s in swaps)


def test_delete_referenced_vehicle_returns_conflict():
    headers = _auth_headers()
    _, vid = _make_station_with_swap(headers, uuid.uuid4().hex[:6])
    resp = client.delete(f"/api/vehicles/{vid}", headers=headers)
    assert resp.status_code == 409, resp.text
    assert "换电历史" in resp.json()["detail"]
    assert client.get(f"/api/vehicles/{vid}", headers=headers).status_code == 200


def test_session_recovers_after_conflict():
    """约束冲突回滚后，会话不处于失败态：紧接着的正常请求仍可完成。"""
    headers = _auth_headers()
    sid, _ = _make_station_with_swap(headers, uuid.uuid4().hex[:6])
    conflict = client.delete(f"/api/stations/{sid}", headers=headers)
    assert conflict.status_code == 409

    follow_up = client.post(
        "/api/stations",
        json={"name": "冲突后新建站", "slot_total": 1, "battery_ready": 0},
        headers=headers,
    )
    assert follow_up.status_code == 201, follow_up.text
    new_id = follow_up.json()["id"]
    listing = client.get("/api/stations", headers=headers)
    assert listing.status_code == 200
    assert any(s["id"] == new_id for s in listing.json())


def test_retired_hidden_by_default_but_queryable_with_history():
    headers = _auth_headers()
    suffix = uuid.uuid4().hex[:6]
    sid, vid = _make_station_with_swap(headers, suffix)
    old_name = f"退役审计站{suffix}"

    # 先改一次名，旧名称必须进入名称历史
    renamed = client.put(f"/api/stations/{sid}", json={"name": "退役后审计新站名"}, headers=headers)
    assert renamed.status_code == 200

    retire = client.post(f"/api/stations/{sid}/retire", headers=headers)
    assert retire.status_code == 200, retire.text
    body = retire.json()
    assert body["is_retired"] is True
    assert body["retired_at"] is not None
    assert any(entry["name"] == old_name for entry in body["name_history"])

    # 默认运营列表隐藏
    default_list = client.get("/api/stations", headers=headers).json()
    assert all(s["id"] != sid for s in default_list)
    # 明确筛选退役可查回
    retired_list = client.get("/api/stations", params={"retired": "true"}, headers=headers).json()
    assert any(s["id"] == sid for s in retired_list)
    all_list = client.get("/api/stations", params={"include_retired": "true"}, headers=headers).json()
    assert any(s["id"] == sid for s in all_list)
    # 按 id 仍可连同历史名称查回
    detail = client.get(f"/api/stations/{sid}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["is_retired"] is True
    assert any(entry["name"] == old_name for entry in detail.json()["name_history"])

    # 车辆同样退役一次
    v_retire = client.post(f"/api/vehicles/{vid}/retire", headers=headers)
    assert v_retire.status_code == 200
    assert v_retire.json()["is_retired"] is True
    assert all(v["id"] != vid for v in client.get("/api/vehicles", headers=headers).json())
    retired_vehicles = client.get("/api/vehicles", params={"retired": "true"}, headers=headers).json()
    assert any(v["id"] == vid for v in retired_vehicles)


def test_retire_is_idempotent_history_unchanged():
    headers = _auth_headers()
    sid, _ = _make_station_with_swap(headers, uuid.uuid4().hex[:6])
    first = client.post(f"/api/stations/{sid}/retire", headers=headers).json()
    second = client.post(f"/api/stations/{sid}/retire", headers=headers)
    assert second.status_code == 200
    again = second.json()
    # 重复退役不改变退役时间与名称历史
    assert again["retired_at"] == first["retired_at"]
    assert again["name_history"] == first["name_history"]


def test_cannot_swap_with_retired_entities():
    headers = _auth_headers()
    sid, vid = _make_station_with_swap(headers, uuid.uuid4().hex[:6])
    assert client.post(f"/api/stations/{sid}/retire", headers=headers).status_code == 200
    assert client.post(f"/api/vehicles/{vid}/retire", headers=headers).status_code == 200
    resp = client.post(
        "/api/swaps",
        json={"vehicle_id": vid, "station_id": sid, "soc_before": 10.0, "soc_after": 90.0},
        headers=headers,
    )
    assert resp.status_code == 422


def test_unreferenced_miscreated_record_can_be_deleted():
    headers = _auth_headers()
    station = client.post(
        "/api/stations",
        json={"name": "误建站", "slot_total": 0, "battery_ready": 0},
        headers=headers,
    ).json()
    vehicle = client.post(
        "/api/vehicles", json={"plate": f"误建{uuid.uuid4().hex[:6]}"}, headers=headers
    ).json()
    # 无任何换电引用：允许真正删除
    assert client.delete(f"/api/stations/{station['id']}", headers=headers).status_code == 204
    assert client.get(f"/api/stations/{station['id']}", headers=headers).status_code == 404
    assert client.delete(f"/api/vehicles/{vehicle['id']}", headers=headers).status_code == 204
    assert client.get(f"/api/vehicles/{vehicle['id']}", headers=headers).status_code == 404


def test_historical_swap_keeps_names_after_retire():
    """退役后历史换电记录仍能带出当时实体的当前名称（外键仍指向保留实体）。"""
    headers = _auth_headers()
    sid, vid = _make_station_with_swap(headers, uuid.uuid4().hex[:6])
    station_name = client.get(f"/api/stations/{sid}", headers=headers).json()["name"]
    vehicle_plate = client.get(f"/api/vehicles/{vid}", headers=headers).json()["plate"]
    client.post(f"/api/stations/{sid}/retire", headers=headers)
    client.post(f"/api/vehicles/{vid}/retire", headers=headers)
    swaps = client.get("/api/swaps", headers=headers).json()
    record = next(s for s in swaps if s["station_id"] == sid and s["vehicle_id"] == vid)
    assert record["station_name"] == station_name
    assert record["vehicle_plate"] == vehicle_plate


def test_integrity_error_rolls_back_and_session_usable():
    """直接验证提交辅助：约束异常转 409 且回滚后会话可继续使用。"""
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.models import Vehicle
    from app.service import commit_or_conflict

    db = SessionLocal()
    try:
        plate = f"冲突{uuid.uuid4().hex[:8]}"
        db.add(Vehicle(plate=plate, model="x"))
        db.commit()
        # 绕过应用层预检，再塞一个同车牌实体，制造唯一约束冲突
        db.add(Vehicle(plate=plate, model="y"))
        try:
            commit_or_conflict(db, "约束冲突")
            assert False, "应当抛出 409"
        except HTTPException as exc:
            assert exc.status_code == 409
        # 回滚干净后，同一会话仍可正常查询
        assert db.query(Vehicle).filter(Vehicle.plate == plate).count() == 1
    finally:
        db.close()
