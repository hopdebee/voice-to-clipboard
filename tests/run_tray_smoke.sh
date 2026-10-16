#!/bin/sh
set -eu
bus_config=$(mktemp /tmp/talk-path-test-bus.XXXXXX)
trap 'rm -f "$bus_config"' EXIT HUP INT TERM
cat > "$bus_config" <<'XML'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*"/>
    <allow receive_sender="*"/>
    <allow own="*"/>
  </policy>
</busconfig>
XML
# No service activation on this isolated test bus: do not start user keyrings/portals.
NO_AT_BRIDGE=1 GIO_USE_VFS=local dbus-run-session --config-file "$bus_config" -- xvfb-run -a /usr/bin/python3 -u tests/smoke_tray.py
