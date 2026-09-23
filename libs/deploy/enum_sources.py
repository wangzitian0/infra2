"""SSOT for service enum sources used by pre-deploy schema checks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EnumSource:
    """Where a service's code-side enum truth lives.

    ``metadata`` names a SQLAlchemy ``MetaData`` as ``module:attribute.path``; every
    native enum column type registered on it is one Postgres enum type (its ``name``)
    with its labels (``enums``). ``imports`` are imported first, for their side effect
    of registering every mapped class on that metadata. ``app_path`` says which
    directory of the service's checkout has to be on ``sys.path``.
    """

    metadata: str
    imports: tuple[str, ...] = ()
    app_path: str = ""


# finance_report's backend is the ``src`` package under apps/backend of the
# finance_report repository — there is no ``finance_report.models.enums`` (the infra2
# ``finance_report`` package is deploy config). Its mapped classes register on
# ``src.database.Base.metadata`` once ``src.orm_registry`` is imported, and every
# Postgres enum there carries an explicit ``name=`` (``account_type_enum``, migration
# 0007) that no class-name derivation reproduces.
ENUM_SOURCES: dict[str, EnumSource] = {
    "finance_report/app": EnumSource(
        metadata="src.database:Base.metadata",
        imports=("src.orm_registry",),
        app_path="<finance_report checkout>/apps/backend",
    ),
}
