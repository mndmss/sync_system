from datetime import datetime
from typing import Annotated
from sqlalchemy import ForeignKey, UniqueConstraint, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base

from sqlalchemy.dialects.postgresql import JSONB


intpk = Annotated[int, mapped_column(primary_key=True)]


class SystemConfigOrm(Base):
    __tablename__ = "system_config"

    id: Mapped[intpk]
    limit: Mapped[int] = mapped_column(default=10)
    sync_max_age: Mapped[int] = mapped_column(default=7)
    sleep_time: Mapped[int] = mapped_column(default=40)


class SourcesOrm(Base):
    __tablename__ = "sources"

    id: Mapped[intpk]
    name: Mapped[str]
    type: Mapped[str]
    api_id: Mapped[int | None]
    just_hear: Mapped[bool] = mapped_column(default=False)

    api_token: Mapped[str | None]


class SourcesDraftOrm(Base):
    __tablename__ = "sources_draft"

    id: Mapped[intpk]
    name: Mapped[str]
    type: Mapped[str]
    api_id: Mapped[int | None]
    just_hear: Mapped[bool] = mapped_column(default=False)
    api_token: Mapped[str | None]


class PostOrm(Base):
    __tablename__ = "posts"

    id: Mapped[intpk]

    content: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    is_deleted: Mapped[bool] = mapped_column(default=False)

    origin_source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE")
    )
    origin_external_post_id: Mapped[int]


    instances: Mapped[list["PostInstanceOrm"]] = relationship(back_populates="post")
    medias: Mapped[list["MediaOrm"]] = relationship(back_populates="post", cascade="all, delete-orphan")


class PostInstanceOrm(Base):
    __tablename__ = "post_instances"

    id: Mapped[intpk]

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))

    external_post_id: Mapped[int]

    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("source_id", "external_post_id"),
    )

    inst_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


    post: Mapped["PostOrm"] = relationship(back_populates="instances")
    media_inst: Mapped[list["MediaInstanceOrm"]] = relationship(back_populates="post_inst", cascade="all, delete-orphan")



class RoutingOrm(Base):
    __tablename__ = "routing"

    id: Mapped[intpk]
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    target_source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))


class PostDeliveriesOrm(Base):
    __tablename__ = "post_deliveries"

    id: Mapped[intpk]
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    target_source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))

    action: Mapped[str]  # create / update
    status: Mapped[str] = mapped_column(default="pending")

    retries: Mapped[int] = mapped_column(default=0)
    origin_source_id: Mapped[int]
    newest_update_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # сюда складываем {'content': '...', 'attachments': [...]}
    payload: Mapped[dict | None] = mapped_column(JSONB)


class MediaOrm(Base):
    __tablename__ = "medias"

    id: Mapped[intpk]
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    local_path: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    type: Mapped[str]


    post: Mapped["PostOrm"] = relationship(back_populates="medias")
    m_instances: Mapped[list["MediaInstanceOrm"]] = relationship(back_populates="media", cascade="all, delete-orphan")


class MediaInstanceOrm(Base):
    __tablename__ = "media_instances"

    id: Mapped[intpk]
    media_id: Mapped[int] = mapped_column(ForeignKey("medias.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    external_media_id: Mapped[str]
    actual_upload_media_id: Mapped[str | None]

    post_instance_id: Mapped[int | None] = mapped_column(ForeignKey("post_instances.id", ondelete="CASCADE"))


    media: Mapped["MediaOrm"] = relationship(back_populates="m_instances")
    post_inst: Mapped["PostInstanceOrm"] = relationship(back_populates="media_inst")
