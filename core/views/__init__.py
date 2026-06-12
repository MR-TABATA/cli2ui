"""View functions, split by domain. Re-exported here so `from core import views`
and `views.<name>` keep working exactly as when this was one module."""
from ._shared import *  # noqa: F401,F403
from .connection import *  # noqa: F401,F403
from .tables import *  # noqa: F401,F403
from .runner import *  # noqa: F401,F403
from .snapshots import *  # noqa: F401,F403
from .ops import *  # noqa: F401,F403
from .objects import *  # noqa: F401,F403
