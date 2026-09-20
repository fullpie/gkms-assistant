"""Single public application identity; V12 remains historical design provenance."""
VERSION = "0.1.0"
DISPLAY_VERSION = "0.1"
PRODUCT = "gkms-assistant"
COMPONENT = "gui"
CHANNEL = "stable"
PLATFORM = "windows-x64"
REPOSITORY = "fullpie/gkms-assistant"


def info():
    return {"product": PRODUCT, "component": COMPONENT, "channel": CHANNEL,
            "platform": PLATFORM, "version": VERSION, "display_version": DISPLAY_VERSION,
            "repository": REPOSITORY}
