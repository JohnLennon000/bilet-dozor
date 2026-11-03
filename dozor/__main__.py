import sys

if len(sys.argv) > 1 and sys.argv[1] == "probe":
    from dozor.probe import main as probe_main

    raise SystemExit(probe_main(sys.argv[2:]))

from dozor.main import main

raise SystemExit(main())
