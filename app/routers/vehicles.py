"""车辆管理路由（需登录）。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..dbops import commit_or_conflict
from ..models import SwapRecord, Vehicle
from ..schemas import VehicleCreate, VehicleOut, VehicleUpdate

router = APIRouter(prefix="/api/vehicles", tags=["车辆"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[VehicleOut])
def list_vehicles(include_retired: bool = False, db: Session = Depends(get_db)):
    # 默认运营列表隐藏退役车辆；审计时显式 include_retired=true 可连同退役实体一起查回
    query = db.query(Vehicle)
    if not include_retired:
        query = query.filter(Vehicle.is_retired.is_(False))
    return query.order_by(Vehicle.id).all()


@router.post("", response_model=VehicleOut, status_code=status.HTTP_201_CREATED)
def create_vehicle(payload: VehicleCreate, db: Session = Depends(get_db)):
    if db.query(Vehicle).filter(Vehicle.plate == payload.plate).first():
        raise HTTPException(status_code=409, detail="车牌已存在")
    vehicle = Vehicle(**payload.model_dump())
    db.add(vehicle)
    commit_or_conflict(db, "车辆保存失败，请稍后重试")
    db.refresh(vehicle)
    return vehicle


@router.get("/{vehicle_id}", response_model=VehicleOut)
def get_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    return vehicle


@router.put("/{vehicle_id}", response_model=VehicleOut)
def update_vehicle(vehicle_id: int, payload: VehicleUpdate, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    if vehicle.is_retired:
        raise HTTPException(status_code=409, detail="车辆已退役，不能再修改")
    data = payload.model_dump(exclude_unset=True)
    if "plate" in data and data["plate"] != vehicle.plate:
        if db.query(Vehicle).filter(Vehicle.plate == data["plate"]).first():
            raise HTTPException(status_code=409, detail="车牌已存在")
    for key, value in data.items():
        setattr(vehicle, key, value)
    commit_or_conflict(db, "车辆更新失败，请稍后重试")
    db.refresh(vehicle)
    return vehicle


@router.post("/{vehicle_id}/retire", response_model=VehicleOut)
def retire_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    """标记退役：保留实体与换电历史，仅从默认运营列表隐藏。

    幂等：对已退役车辆重复退役不改变历史车牌与退役时间，直接返回当前状态。
    """
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    if not vehicle.is_retired:
        vehicle.is_retired = True
        vehicle.retired_at = datetime.utcnow()
        vehicle.retired_plate = vehicle.plate
        commit_or_conflict(db, "车辆退役失败，请稍后重试")
        db.refresh(vehicle)
    return vehicle


@router.delete("/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    # 存在换电历史时不得物理删除，返回稳定的冲突结果，提示先走退役流程
    if db.query(SwapRecord.id).filter(SwapRecord.vehicle_id == vehicle_id).first():
        raise HTTPException(status_code=409, detail="该车辆存在换电历史，不能删除，请改用退役处理")
    db.delete(vehicle)
    commit_or_conflict(db, "该车辆存在关联数据，不能删除，请改用退役处理")
    return None
