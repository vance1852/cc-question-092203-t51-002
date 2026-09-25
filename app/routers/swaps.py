"""换电记录路由（需登录）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..dbops import commit_or_conflict
from ..models import Station, SwapRecord, Vehicle
from ..schemas import SwapCreate, SwapOut

router = APIRouter(prefix="/api/swaps", tags=["换电记录"], dependencies=[Depends(get_current_user)])


def _to_out(record: SwapRecord) -> SwapOut:
    # 退役实体不会被物理删除，关联历史始终能解析出名称；
    # 退役时已把当时的名称/车牌快照到 retired_name/retired_plate，审计以快照为准
    if record.station is not None:
        station_name = record.station.retired_name or record.station.name
    else:
        station_name = None
    if record.vehicle is not None:
        vehicle_plate = record.vehicle.retired_plate or record.vehicle.plate
    else:
        vehicle_plate = None
    return SwapOut(
        id=record.id,
        vehicle_id=record.vehicle_id,
        station_id=record.station_id,
        soc_before=record.soc_before,
        soc_after=record.soc_after,
        swapped_at=record.swapped_at,
        vehicle_plate=vehicle_plate,
        station_name=station_name,
    )


@router.get("", response_model=list[SwapOut])
def list_swaps(db: Session = Depends(get_db)):
    records = db.query(SwapRecord).order_by(SwapRecord.swapped_at.desc()).all()
    return [_to_out(r) for r in records]


@router.post("", response_model=SwapOut, status_code=status.HTTP_201_CREATED)
def create_swap(payload: SwapCreate, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, payload.vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    station = db.get(Station, payload.station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    if vehicle.is_retired:
        raise HTTPException(status_code=409, detail="车辆已退役，不能再产生换电记录")
    if station.is_retired:
        raise HTTPException(status_code=409, detail="换电站已退役，不能再产生换电记录")
    if station.battery_ready <= 0:
        raise HTTPException(status_code=422, detail="该换电站暂无满电电池可换")
    if payload.soc_after <= payload.soc_before:
        raise HTTPException(status_code=422, detail="换电后电量应高于换电前电量")

    record = SwapRecord(
        vehicle_id=payload.vehicle_id,
        station_id=payload.station_id,
        soc_before=payload.soc_before,
        soc_after=payload.soc_after,
    )
    # 换电后更新车辆电量、扣减站点可用电池（与记录插入同一事务，失败整体回滚）
    vehicle.current_soc = payload.soc_after
    station.battery_ready -= 1
    db.add(record)
    commit_or_conflict(db, "换电记录保存失败，请稍后重试")
    db.refresh(record)
    return _to_out(record)
