"""退役/删除流程共享的事务辅助。"""
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


def commit_or_conflict(db: Session, detail: str) -> None:
    """提交事务；任何数据库约束异常都回滚并转换为稳定的 409 冲突响应。

    回滚后会话恢复到干净状态，保证下一次正常请求仍可完成，
    绝不让会话停留在失败状态，也不留下半次更新。
    """
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=409, detail=detail)
