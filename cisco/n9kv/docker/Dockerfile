FROM ghcr.io/srl-labs/vrnetlab-base:0.2.1

RUN apt-get update -qy \
   && apt-get install -y --no-install-recommends \
   udev vsftpd ftp wget

ARG IMAGE
COPY $IMAGE* /
COPY OVMF.fd /
COPY *.py /
