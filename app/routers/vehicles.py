"""车辆管理路由（需登录）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import SwapRecord, Vehicle
from ..schemas import VehicleCreate, VehicleOut, VehicleUpdate
from ..service import append_name_history, commit_or_conflict, delete_if_unreferenced, retire_entity

router = APIRouter(prefix="/api/vehicles", tags=["车辆"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[VehicleOut])
def list_vehicles(
    retired: bool = False,
    include_retired: bool = False,
    db: Session = Depends(get_db),
):
    """运营列表。默认只返回在役车辆：

    - retired=true：明确只查退役车辆（审计用）；
    - include_retired=true：在役与退役一并返回；
    - 默认：退役车辆从运营列表隐藏。
    """
    query = db.query(Vehicle)
    if retired:
        query = query.filter(Vehicle.is_retired.is_(True))
    elif not include_retired:
        query = query.filter(Vehicle.is_retired.is_(False))
    return query.order_by(Vehicle.id).all()


@router.post("", response_model=VehicleOut, status_code=status.HTTP_201_CREATED)
def create_vehicle(payload: VehicleCreate, db: Session = Depends(get_db)):
    if db.query(Vehicle).filter(Vehicle.plate == payload.plate).first():
        raise HTTPException(status_code=409, detail="车牌已存在")
    vehicle = Vehicle(**payload.model_dump())
    db.add(vehicle)
    commit_or_conflict(db, "车牌已存在或数据约束冲突，车辆创建失败")
    db.refresh(vehicle)
    return vehicle


@router.get("/{vehicle_id}", response_model=VehicleOut)
def get_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    # 退役车辆仍可按 id 查回，连同名称历史供审计人员核对
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    return vehicle


@router.put("/{vehicle_id}", response_model=VehicleOut)
def update_vehicle(vehicle_id: int, payload: VehicleUpdate, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    data = payload.model_dump(exclude_unset=True)
    if "plate" in data and data["plate"] != vehicle.plate:
        if db.query(Vehicle).filter(Vehicle.plate == data["plate"]).first():
            raise HTTPException(status_code=409, detail="车牌已存在")
        # 先留存旧车牌再赋新车牌，名称历史只增不改
        append_name_history(vehicle, data["plate"])
    for key, value in data.items():
        setattr(vehicle, key, value)
    commit_or_conflict(db, "车辆更新失败，可能存在数据约束冲突")
    db.refresh(vehicle)
    return vehicle


@router.post("/{vehicle_id}/retire", response_model=VehicleOut)
def retire_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    """把车辆标记为退役（可审计的软删除，幂等）。"""
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    return retire_entity(db, vehicle, "车辆")


@router.delete("/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vehicle(vehicle_id: int, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    # 存在换电历史时拒绝物理删除；无引用的误建数据才真正删除
    delete_if_unreferenced(db, vehicle, SwapRecord.vehicle_id, "车辆")
    return None
