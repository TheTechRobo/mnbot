import os

# Update this whenever you make a change, cosmetic or not.
# During development you can ignore it, but when you actually
# push it to prod, it *must* be updated.
VERSION = "20250115.01"

DEBUG = os.environ.get("DEBUG") == "1"
if DEBUG:
    VERSION += "-debug"
