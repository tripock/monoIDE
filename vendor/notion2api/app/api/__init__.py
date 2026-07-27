"""API routers.

Importing this package also installs monoIDE's custom-agent hook. It has to run
here, before ``app.api.chat`` imports the transcript builders into its own
namespace, otherwise patching them later would have no effect.
"""

try:  # a broken hook must never stop the server from booting
    from app.agent_override import install as _install_agent_override

    _install_agent_override()
except Exception:  # pragma: no cover - diagnostics only
    import logging

    logging.getLogger(__name__).warning(
        "custom-agent hook not installed; using the default Notion assistant",
        exc_info=True,
    )
