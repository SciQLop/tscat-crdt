from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass

from anyio import Lock, Path
from fastapi import Depends
from fastapi_users.db import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from fastapi_users_db_sqlalchemy.access_token import (
    SQLAlchemyBaseAccessTokenTableUUID,
)
from sqlalchemy import JSON, Column
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    rooms = Column(JSON, default=[], nullable=False)


class AccessToken(SQLAlchemyBaseAccessTokenTableUUID, Base):
    pass


@dataclass
class DB:
    async_session_maker: async_sessionmaker[AsyncSession]
    create_db_and_tables: Callable[[], Awaitable]
    get_async_session: Callable[[], AsyncGenerator[AsyncSession, None]]
    get_user_db: Callable[[AsyncSession], AsyncGenerator[SQLAlchemyUserDatabase, None]]


def get_db(path: str) -> DB:
    database_url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(database_url)
    async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
    lock = Lock()

    async def create_db_and_tables():
        async with lock:
            if await Path(path).exists():
                return

            async with engine.begin() as conn:  # pragma: nocover
                await conn.run_sync(Base.metadata.create_all)

    async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
        async with async_session_maker() as session:
            yield session

    async def get_user_db(
        session: AsyncSession = Depends(get_async_session),
    ) -> AsyncGenerator[SQLAlchemyUserDatabase]:
        yield SQLAlchemyUserDatabase(session, User)

    return DB(
        async_session_maker=async_session_maker,
        create_db_and_tables=create_db_and_tables,
        get_async_session=get_async_session,
        get_user_db=get_user_db,
    )
