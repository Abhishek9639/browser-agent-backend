"""Shared schema configuration.

Postgres is snake_case, the wire is camelCase. Doing the translation once here
keeps every response consistent with the section 6.1 event bodies, which the
frontend already expects in camelCase.
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

__all__ = ["CamelModel"]


class CamelModel(BaseModel):
    """Serialises to camelCase, accepts either spelling on the way in."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
