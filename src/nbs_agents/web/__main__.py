"""
``nbs-agents-web``: serve the agent log web UI on localhost.

Connection settings come from the nbs-cli config (``--config``,
``$NBS_CLI_CONFIG`` or ``config/nbs-cli.toml`` under ``$NBS_AGENTS_HOME``, and
``NBS_*`` variables).

The page needs your nbs-agents token (``config/token`` under ``$NBS_AGENTS_HOME``, made if
missing): the URL printed at start carries it, like Jupyter's.
"""

import argparse
import logging

from ..assistant.attachments import AttachmentStore
from ..assistant.admin import add_assistant_only_argument, assistant_only
from ..assistant.auth import ensure_token
from nbs_agents.paths import BeamlineError, add_beamline_argument, use_beamline
from ..cli.config import CliConfig
from ..cli.core import Beamline
from .app import LOCAL_HOSTS, create_app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="nbs-agents-web", description=__doc__.strip().splitlines()[0])
    parser.add_argument("--config", help="nbs-cli TOML settings file")
    parser.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="extra host name the page may be reached by (repeatable); localhost names are always allowed",
    )
    parser.add_argument(
        "--attachments",
        help="folder of figures that messages link as att:ID (default: nbs-cli attachments_dir, "
        "$NBS_ATTACHMENTS_DIR or data/attachments under $NBS_AGENTS_HOME)",
    )
    add_beamline_argument(parser)
    add_assistant_only_argument(parser)
    args = parser.parse_args(argv)
    if args.assistant_only:
        assistant_only()
    try:
        use_beamline(args.beamline)
    except BeamlineError as exc:
        parser.error(str(exc))

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    token = ensure_token()
    beamline = Beamline(CliConfig.load(args.config))
    app = create_app(
        beamline,
        allowed_hosts=LOCAL_HOSTS + tuple(args.allow_host),
        attachments=AttachmentStore(args.attachments or beamline.config.attachments_dir or None),
        token=token,
    )
    base = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}"
    print(f"nbs-agents-web on {base}\nOpen {base}/?token={token} to sign in (only you can read this token)", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
