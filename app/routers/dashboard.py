"""仪表盘统计路由（需登录）。"""
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Station, SwapRecord, Vehicle
from ..schemas import DashboardStats

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"], dependencies=[Depends(get_current_user)])


@router.get("/stats", response_model=DashboardStats)
def stats(db: Session = Depends(get_db)):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    # 运营统计只计在役实体，退役实体保留数据供审计但不进入运营指标
    active_stations = db.query(Station).filter(Station.is_retired.is_(False))
    active_vehicles = db.query(Vehicle).filter(Vehicle.is_retired.is_(False))
    return DashboardStats(
        station_total=active_stations.count(),
        station_running=active_stations.filter(Station.status == "running").count(),
        vehicle_total=active_vehicles.count(),
        vehicle_fault=active_vehicles.filter(Vehicle.status == "fault").count(),
        swap_today=db.query(SwapRecord).filter(SwapRecord.swapped_at >= today_start).count(),
        battery_ready_total=db.query(func.coalesce(func.sum(Station.battery_ready), 0))
        .filter(Station.is_retired.is_(False))
        .scalar()
        or 0,
    )
