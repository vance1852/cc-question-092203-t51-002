"""站点/车辆退役生命周期的共享业务逻辑。

退役（retire）是可审计的软删除：
- 存在换电历史的实体不得物理删除，返回 409 冲突；
- 退役后从默认运营列表隐藏，但可通过明确筛选连同名称历史查回；
- 退役幂等：重复退役不改变退役时间与名称历史；
- 未被换电记录引用的误建数据才允许真正删除；
- 任何数据库约束异常都会回滚并转换成一致的 409 响应，
  保证会话不处于失败状态、也不会留下半次更新。
"""
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .models import SwapRecord


def commit_or_conflict(db: Session, conflict_message: str) -> None:
    """提交事务；遇到数据库约束异常时整体回滚并抛出一致的 409。

    回滚后会话恢复干净状态，下一次正常请求仍可在同一会话上完成。
    """
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=conflict_message)


def has_swap_history(db: Session, column: Any, entity_id: int) -> bool:
    """实体是否被换电记录引用（含历史记录）。"""
    return db.query(SwapRecord.id).filter(column == entity_id).first() is not None


def append_name_history(entity: Any, new_name: str) -> None:
    """名称发生改变时，把旧名称追加进名称历史（应在赋值新名称之前调用）。

    名称历史只增不改，退役后也能据此查回实体的曾用名；
    名称未变（如重复提交）时不追加。
    """
    if new_name == entity.name:
        return
    history = list(entity.name_history or [])
    history.append({"name": entity.name, "changed_at": datetime.utcnow().isoformat()})
    entity.name_history = history


def retire_entity(db: Session, entity: Any, word: str) -> Any:
    """把实体标记为退役。幂等：已退役则原样返回，不改变退役时间与历史。

    word 用于冲突提示，如 "换电站" / "车辆"。
    """
    if not entity.is_retired:
        entity.is_retired = True
        entity.retired_at = datetime.utcnow()
        commit_or_conflict(db, f"该{word}退役失败，可能存在数据约束冲突")
        db.refresh(entity)
    return entity


def delete_if_unreferenced(
    db: Session, entity: Any, column: Any, word: str
) -> None:
    """删除实体；若仍被换电记录引用则拒绝并返回 409。

    即便应用层检查与数据库约束之间出现竞态，commit 时的外键异常
    同样会被回滚并转换为一致响应，绝不留下半次删除。
    """
    entity_id = entity.id
    if has_swap_history(db, column, entity_id):
        raise HTTPException(
            status_code=409,
            detail=f"该{word}存在换电历史，不能删除；如需停用请将其标记为退役",
        )
    db.delete(entity)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"该{word}仍被业务记录引用，不能删除；如需停用请将其标记为退役",
        )
