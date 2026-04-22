"""Root conftest — re-exports the `postgres_engine` fixture so Category A
tests in any subpackage (`lavandula/{reports,nonprofits,common}/tests/`)
can request it."""
from lavandula.common.tests.conftest import postgres_engine  # noqa: F401
