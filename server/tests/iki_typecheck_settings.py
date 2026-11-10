"""Settings mypy's Django plugin loads: the test settings, without the generated fixture plugins.

The plugins' code is generated, so it isn't type-checked, and building it
just to type-check would make mypy run Django's migration machinery.
"""

import os

os.environ["IKI_TEST_PLUGINS"] = "0"

from iki_test_settings import *  # noqa: F403
