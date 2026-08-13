# webadmin/db.py
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webadmin.config import DATABASE_CONNECT_ARGS, DATABASE_URL

# pool_pre_ping: validate a pooled connection before handing it out and
# transparently reconnect if the remote server (or a proxy/firewall on the
# network path to it) has closed it while idle — without this, the first request
# after an idle spell dies with asyncpg "connection is closed".
# pool_recycle: proactively drop connections older than 5 min so they never
# reach that idle-drop timeout in the first place.
engine = create_async_engine(
    DATABASE_URL,
    connect_args=DATABASE_CONNECT_ARGS,
    pool_pre_ping=True,
    pool_recycle=300,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
