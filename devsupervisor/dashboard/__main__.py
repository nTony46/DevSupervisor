"""`python3 -m devsupervisor.dashboard` — the same thing `devsup dashboard` runs."""

import sys

from ..cli import main

if __name__ == "__main__":
    sys.exit(main(["dashboard", *sys.argv[1:]]))
