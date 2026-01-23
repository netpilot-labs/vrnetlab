# Cisco IOL (IOS on Linux)

This is the containerlab/vrnetlab image for Cisco IOL (IOS On Linux).

CML recently introduced IOL-XE which compared to other Cisco images, runs very lightly since it executes purely as a binary and has no requirement for a virtualisation layer.

There are two types of IOL you can obtain:

- IOL, meant for Layer 3 operation as a router.
- IOL-L2, meant to act as a L2/L2+ switch.

## Building the image

Copy the `x86_64_crb_linux-adventerprisek9-ms` into this directory and rename it to `cisco_iol-x.y.z.bin` (x.y.z being the version number). For example `cisco_iol-17.12.01.bin`. The `.bin` extension is important.

> If using IOL-L2 it is recommended to name your image to identify it as IOL-L2. For example: `cisco_iol-L2-x.y.z.bin`

> If you are getting the image from the CML refplat, the IOL image is under the `iol-xe-x.y.z` directory or `ioll2-xe-x.y.z` for IOL-L2.

### Build command

Execute

```
make docker-image
```

and the image will be built and tagged. You can view the image by executing `docker images`.

```
containerlab@containerlab:~$ docker images
REPOSITORY                      TAG           IMAGE ID       CREATED          SIZE
vrnetlab/cisco_iol              L2-17.12.01   c207d920446e   5 seconds ago    607MB
vrnetlab/cisco_iol              17.12.01      30be6c875c80   12 minutes ago   704MB
```

## Usage

You can define the image easily and use it in a topology.

```yaml
# topology.clab.yaml
name: mylab
topology:
  nodes:
    iol:
      kind: cisco_iol
      image: vrnetlab/cisco_iol:<tag>
```

**IOL-L2**

```yaml
# topology.clab.yaml
name: mylab
topology:
  nodes:
    iol:
      kind: cisco_iol
      image: vrnetlab/cisco_iol:<tag>
      type: l2
```
