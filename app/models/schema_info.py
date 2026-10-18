"""Schema version marker for the public EZ Mark database.

The public baseline stores a single row in ``schema_info`` identifying the
database as an EZ Mark public schema. Databases without this marker are not
recognized by the application and are refused at startup.
"""

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SchemaInfo(Base):
    __tablename__ = "schema_info"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
