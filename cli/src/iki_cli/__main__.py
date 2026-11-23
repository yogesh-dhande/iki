"""`python -m iki_cli`, the same as the `iki` command."""

import sys

from iki_cli.main import main

if __name__ == "__main__":
    sys.exit(main())
