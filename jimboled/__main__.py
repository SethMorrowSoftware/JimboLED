"""Run JimboLED: ``python -m jimboled`` (used by the systemd unit)."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from . import __version__, create_app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jimboled", description="WLED + GPIO dashboard")
    parser.add_argument("--host", default=None, help="bind address (default from config, 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="port (default from config, 80)")
    parser.add_argument("--data-dir", default=None, help="where config.json lives (default $JIMBOLED_DATA_DIR)")
    parser.add_argument("--debug", action="store_true", help="Flask debug server with auto-reload")
    parser.add_argument("--threads", type=int, default=int(os.environ.get("JIMBOLED_THREADS", "8")))
    parser.add_argument("--version", action="version", version=f"JimboLED {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    app = create_app(args.data_dir)
    store = app.extensions["jimboled"].store
    server_cfg = store.section("server") or {}
    host = args.host or os.environ.get("JIMBOLED_HOST") or server_cfg.get("host") or "0.0.0.0"
    port = args.port or int(os.environ.get("JIMBOLED_PORT") or server_cfg.get("port") or 80)

    def _shutdown(signum, _frame):
        logging.getLogger("jimboled").info("signal %s received, releasing relays", signum)
        ctx = app.extensions["jimboled"]
        # Relays first, and each step on its own: a failure tidying up the WLED
        # poller must not be the reason a bed motor stays energised.
        try:
            ctx.gpio.stop()
        finally:
            try:
                ctx.devices.stop()
            except Exception:
                logging.getLogger("jimboled").exception("error stopping the device poller")
            try:
                ctx.discovery.listener.stop()
            except Exception:
                pass
            sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logging.getLogger("jimboled").info("JimboLED %s listening on http://%s:%s", __version__, host, port)
    if args.debug:
        app.run(host=host, port=port, debug=True, use_reloader=False, threaded=True)
        return 0
    from waitress import serve

    serve(app, host=host, port=port, threads=args.threads, ident="JimboLED", channel_timeout=60,
          connection_limit=200, cleanup_interval=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
