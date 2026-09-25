"""换电站管理路由（需登录）。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..dbops import commit_or_conflict
from ..models import Station, SwapRecord, User
from ..schemas import StationCreate, StationOut, StationUpdate

router = APIRouter(prefix="/api/stations", tags=["换电站"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[StationOut])
def list_stations(include_retired: bool = False, db: Session = Depends(get_db)):
    # 默认运营列表隐藏退役站点；审计时显式 include_retired=true 可连同退役实体一起查回
    query = db.query(Station)
    if not include_retired:
        query = query.filter(Station.is_retired.is_(False))
    return query.order_by(Station.id).all()


@router.post("", response_model=StationOut, status_code=status.HTTP_201_CREATED)
def create_station(payload: StationCreate, db: Session = Depends(get_db)):
    if payload.battery_ready > payload.slot_total:
        raise HTTPException(status_code=422, detail="满电电池数不能超过仓位总数")
    station = Station(**payload.model_dump())
    db.add(station)
    commit_or_conflict(db, "换电站保存失败，请稍后重试")
    db.refresh(station)
    return station


@router.get("/{station_id}", response_model=StationOut)
def get_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    return station


@router.put("/{station_id}", response_model=StationOut)
def update_station(station_id: int, payload: StationUpdate, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    if station.is_retired:
        raise HTTPException(status_code=409, detail="换电站已退役，不能再修改")
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(station, key, value)
    if station.battery_ready > station.slot_total:
        raise HTTPException(status_code=422, detail="满电电池数不能超过仓位总数")
    commit_or_conflict(db, "换电站更新失败，请稍后重试")
    db.refresh(station)
    return station


@router.post("/{station_id}/retire", response_model=StationOut)
def retire_station(station_id: int, db: Session = Depends(get_db)):
    """标记退役：保留实体与换电历史，仅从默认运营列表隐藏。

    幂等：对已退役站点重复退役不改变历史名称与退役时间，直接返回当前状态。
    """
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    if not station.is_retired:
        station.is_retired = True
        station.retired_at = datetime.utcnow()
        station.retired_name = station.name
        commit_or_conflict(db, "换电站退役失败，请稍后重试")
        db.refresh(station)
    return station


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    # 存在换电历史时不得物理删除，返回稳定的冲突结果，提示先走退役流程
    if db.query(SwapRecord.id).filter(SwapRecord.station_id == station_id).first():
        raise HTTPException(status_code=409, detail="该换电站存在换电历史，不能删除，请改用退役处理")
    db.delete(station)
    commit_or_conflict(db, "该换电站存在关联数据，不能删除，请改用退役处理")
    return None
