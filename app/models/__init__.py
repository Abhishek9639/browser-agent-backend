"""ORM models.

Importing this package must register every model on ``Base.metadata``; Alembic's
``env.py`` relies on that for autogenerate. Each new model module is imported
here, and listed in ``__all__``.

The nine tables from spec section 5 arrive in the part that introduces them.
"""

from app.db.base import Base

__all__ = ["Base"]
