"""python -m smppclient [server options]  |  python -m smppclient mock [mock options]"""
import sys

if len(sys.argv) > 1 and sys.argv[1] == "mock":
    del sys.argv[1]
    from .mock_smsc import main
else:
    from .server import main

main()
