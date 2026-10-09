import os
import sys


def main():
    if len(sys.argv) == 1 and not os.environ.get("_MC_COMPLETE"):
        # A bare `mc` is the dashboard: go straight to it without importing typer and building the whole command tree first
        # (about 60 ms). Anything that goes wrong is reported the way the command line reports it.
        try:
            from .tui.app import run as dashboard
            return dashboard(None)
        except (SystemExit, GeneratorExit):
            raise
        except BaseException as e:                        # noqa: BLE001 - Fail, Ctrl-C, or a bug: same handling as every command
            from .cli import handle_error
            handle_error(e, [])
    from .cli import run
    run()


main()
