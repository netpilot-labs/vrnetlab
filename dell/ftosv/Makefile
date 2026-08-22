VENDOR=Dell
NAME=ftosv
IMAGE_FORMAT=qcow2
IMAGE_GLOB=*.qcow2

# Canonical image name: dellftos.{version}.qcow2 — any octet widths
# (dellftos.10.6.1.1.qcow2, dellftos.10.5.4.0.98.qcow2). The old fixed-width
# regex truncated 5-part versions and errored on multi-digit octets; with
# strip-prefix/suffix a mis-named file leaves VERSION==IMAGE and the
# makefile-sanity check fails loud instead.
VERSION=$(shell echo $(IMAGE) | sed -e 's/^dellftos\.//' -e 's/\.qcow2$$//')

-include ../../makefile-sanity.include
-include ../../makefile.include

