# Third-party notices

Clickwarden does not copy or bundle third-party source code. It imports the libraries
below at runtime; install them with `pip install -e .`. Licenses are those declared in
each package's published metadata for the versions tested (check the package itself
for the authoritative text).

| Package | Tested version | License |
|---|---|---|
| [requests](https://pypi.org/project/requests/) | 2.33 | Apache-2.0 |
| [mss](https://pypi.org/project/mss/) | 10.2 | MIT |
| [Pillow](https://pypi.org/project/pillow/) | 12.3 | MIT-CMU (HPND-style) |
| [NumPy](https://pypi.org/project/numpy/) | 2.4 | BSD-3-Clause (plus bundled permissive components) |
| [python-dotenv](https://pypi.org/project/python-dotenv/) | 1.2 | BSD-3-Clause |
| [mcp](https://pypi.org/project/mcp/) | 2.3 | MIT |
| [pywin32](https://pypi.org/project/pywin32/) | 312 | PSF |

All are permissive licenses compatible with Apache-2.0. Vision models and the online
services you connect to are *not* part of Clickwarden; their own terms apply to your use.
