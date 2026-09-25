"""换电站管理路由（需登录）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Station, SwapRecord
from ..schemas import StationCreate, StationOut, StationUpdate
from ..service import append_name_history, commit_or_conflict, delete_if_unreferenced, retire_entity

router = APIRouter(prefix="/api/stations", tags=["换电站"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[StationOut])
def list_stations(
    retired: bool = False,
    include_retired: bool = False,
    db: Session = Depends(get_db),
):
    """运营列表。默认只返回在役站点：

    - retired=true：明确只查退役站点（审计用）；
    - include_retired=true：在役与退役一并返回；
    - 默认：退役站点从运营列表隐藏。
    """
    query = db.query(Station)
    if retired:
        query = query.filter(Station.is_retired.is_(True))
    elif not include_retired:
        query = query.filter(Station.is_retired.is_(False))
    return query.order_by(Station.id).all()


@router.post("", response_model=StationOut, status_code=status.HTTP_201_CREATED)
def create_station(payload: StationCreate, db: Session = Depends(get_db)):
    if payload.battery_ready > payload.slot_total:
        raise HTTPException(status_code=422, detail="满电电池数不能超过仓位总数")
    station = Station(**payload.model_dump())
    db.add(station)
    commit_or_conflict(db, "换电站创建失败，可能存在数据约束冲突")
    db.refresh(station)
    return station


@router.get("/{station_id}", response_model=StationOut)
def get_station(station_id: int, db: Session = Depends(get_db)):
    # 退役站点仍可按 id 查回，连同名称历史供审计人员核对
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    return station


@router.put("/{station_id}", response_model=StationOut)
def update_station(station_id: int, payload: StationUpdate, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        # 先留存旧名再赋新名，名称历史只增不改
        append_name_history(station, data["name"])
    for key, value in data.items():
        setattr(station, key, value)
    if station.battery_ready > station.slot_total:
        raise HTTPException(status_code=422, detail="满电电池数不能超过仓位总数")
    commit_or_conflict(db, "换电站更新失败，可能存在数据约束冲突")
    db.refresh(station)
    return station


@router.post("/{station_id}/retire", response_model=StationOut)
def retire_station(station_id: int, db: Session = Depends(get_db)):
    """把站点标记为退役（可审计的软删除，幂等）。"""
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    return retire_entity(db, station, "换电站")


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    # 存在换电历史时拒绝物理删除；无引用的误建数据才真正删除
    delete_if_unreferenced(db, station, SwapRecord.station_id, "换电站")
    return None
