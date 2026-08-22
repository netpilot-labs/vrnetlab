VENDOR=Dell
NAME=ftosv
IMAGE_FORMAT=qcow2
IMAGE_GLOB=*.qcow2

# Canonical image name: dellftos.{version}.qcow2 — any octet widths
# (dellftos.10.6.1.1.qcow2, dellftos.10.5.4.0.98.qcow2). The old fixed-width
# regex truncated 5-part versions and errored on multi-digit octets. One
# whole-name substitution: anything not matching the canonical form passes
# through unchanged, so VERSION==IMAGE and the docker-build sanity check
# fails loud (two separate strips would tag `random.qcow2` as `random`).
VERSION=$(shell echo $(IMAGE) | sed -e 's/^dellftos\.\(..*\)\.qcow2$$/\1/')

-include ../../makefile-sanity.include
-include ../../makefile.include

