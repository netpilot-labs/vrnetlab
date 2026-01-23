#!/bin/bash

IOL_PID=${IOL_PID:-1}

echo "Launching IOL with PID" $IOL_PID

# ============================================
# Detect IOL type and version
# ============================================

# Detect L2 switch from /iol/iol_type file (set at docker build time based on filename)
IS_L2_IMAGE=false
if [ -f /iol/iol_type ] && grep -qi "L2" /iol/iol_type; then
    IS_L2_IMAGE=true
    echo "Detected L2 switch image (from iol_type file)"
else
    echo "Detected L3 router image"
fi

# Detect IOL version based on binary architecture
# IOL 15.x = 32-bit ELF, IOL 17.x = 64-bit ELF
# Check ELF header: byte 5 is 1 for 32-bit, 2 for 64-bit
IS_LEGACY_IOL=false
ELF_CLASS=$(od -An -t x1 -j 4 -N 1 /iol/iol.bin 2>/dev/null | tr -d ' ')
if [ "$ELF_CLASS" = "01" ]; then
    IS_LEGACY_IOL=true
    echo "Detected legacy IOL (32-bit) - using enhanced startup"
else
    echo "Detected modern IOL (64-bit) - using standard startup"
fi

# ============================================
# L2 15.x: Strip VRF and add "no switchport" to Ethernet0/0
# (IOL L2 15.x doesn't support VRF, needs routed port for mgmt IP)
# ============================================
transform_l2_config() {
    local input_file="$1"
    local output_file="$2"

    echo "Transforming config for L2 15.x switch (strip VRF, add 'no switchport')..."

    # 1. Remove VRF definition block
    # 2. Remove "vrf forwarding" lines
    # 3. Convert VRF routes to global routes
    # 4. Add "no switchport" before "ip address" on Ethernet0/0
    sed -e '/^vrf definition/,/^!/d' \
        -e '/^ vrf forwarding/d' \
        -e 's/ip route vrf [^ ]* /ip route /g' \
        -e 's/ipv6 route vrf [^ ]* /ipv6 route /g' \
        "$input_file" | \
    awk '
    /^interface Ethernet0\/0/ { in_eth0=1; print; next }
    in_eth0 && /^ ip address/ { print " no switchport"; print; in_eth0=0; next }
    in_eth0 && /^!/ { in_eth0=0 }
    { print }
    ' > "$output_file"

    echo "Config transformed (VRF removed, 'no switchport' added)"
    return 0
}

# ============================================
# IOL 15.x (32-bit) - Requires special handling
# ============================================
if [ "$IS_LEGACY_IOL" = true ]; then

    # Generate IOL license (required for 15.x)
    export HOME=/iol
    export IOURC=/iol/.iourc
    python3 /gen_license.py

    if [ -f /iol/.iourc ]; then
        echo "License file created:"
        cat /iol/.iourc
    else
        echo "WARNING: Failed to create license file"
    fi

    # EEM applet to generate SSH keys after boot
    # IOL 15.x ignores "crypto key generate rsa" in startup-config
    # Using syslog trigger for faster SSH (~2s vs 60s with @reboot cron)
    EEM_APPLET='!
event manager session cli username admin
event manager applet EEM_SSH_Keygen
 event syslog occurs 1 pattern "%SYS-5-RESTART"
 action 0.0 info type routername
 action 0.1 set status "none"
 action 1.0 cli command "enable"
 action 2.0 cli command "show ip ssh | include ^SSH"
 action 2.1 regexp "([ED][^ ]+)" "$_cli_result" result status
 action 3.0 if $status eq Disabled
 action 3.1  cli command "configure terminal"
 action 3.2  cli command "crypto key generate rsa modulus 2048 label $_info_routername"
 action 3.3  cli command "end"
 action 3.4 end
!'

    # Process config for 15.x
    if [ -f /iol/config.txt ]; then
        echo "Found config.txt, processing for 15.x..."
        echo "Original config.txt:"
        cat /iol/config.txt

        # For L2 15.x: Strip VRF and add "no switchport"
        if [ "$IS_L2_IMAGE" = true ]; then
            transform_l2_config /iol/config.txt /tmp/config_transformed.txt
            CONFIG_FILE="/tmp/config_transformed.txt"
            echo "Transformed config:"
            cat "$CONFIG_FILE"
        else
            CONFIG_FILE="/iol/config.txt"
        fi

        # Check if EEM applet already present
        if grep -q "event manager applet EEM_SSH_Keygen" "$CONFIG_FILE"; then
            echo "EEM applet already present in config"
            cp "$CONFIG_FILE" /tmp/config_with_eem.txt
        else
            # Insert EEM applet before the final "end" statement
            sed '/^end$/d' "$CONFIG_FILE" > /tmp/config_with_eem.txt
            echo "$EEM_APPLET" >> /tmp/config_with_eem.txt
            echo "end" >> /tmp/config_with_eem.txt
            echo "Injected EEM applet for SSH key generation"
        fi

        # Inject into NVRAM (for IOL versions that read NVRAM)
        /iou_import -c 64 /iol/nvram_$(printf "%05d" $IOL_PID) /tmp/config_with_eem.txt
        if [ $? -eq 0 ]; then
            echo "Successfully injected config into NVRAM"
        else
            echo "WARNING: Failed to inject config into NVRAM"
        fi

        # Also update config.txt (for IOL versions that read -c flag)
        # Keep both in sync - no downside, ensures compatibility with all 15.x variants
        cp /tmp/config_with_eem.txt /iol/config.txt
        echo "Updated config.txt (keeping NVRAM and config.txt in sync)"
    else
        echo "No config.txt found, creating minimal config with SSH support..."

        # Minimal config (same for L2 and L3 without config.txt)
        cat > /tmp/minimal.cfg << 'MINEOF'
!
hostname Router
ip domain name local
username admin privilege 15 secret admin
!
event manager session cli username admin
event manager applet EEM_SSH_Keygen
 event syslog occurs 1 pattern "%SYS-5-RESTART"
 action 0.0 info type routername
 action 0.1 set status "none"
 action 1.0 cli command "enable"
 action 2.0 cli command "show ip ssh | include ^SSH"
 action 2.1 regexp "([ED][^ ]+)" "$_cli_result" result status
 action 3.0 if $status eq Disabled
 action 3.1  cli command "configure terminal"
 action 3.2  cli command "crypto key generate rsa modulus 2048 label $_info_routername"
 action 3.3  cli command "end"
 action 3.4 end
!
ip ssh version 2
line vty 0 4
 login local
 transport input ssh
!
end
MINEOF
        /iou_import -c 64 /iol/nvram_$(printf "%05d" $IOL_PID) /tmp/minimal.cfg
        cp /tmp/minimal.cfg /iol/config.txt
    fi
else
    # ============================================
    # IOL 17.x (64-bit) - Standard startup
    # ============================================
    # No license needed, -c flag works, crypto key works in startup-config
    echo "Using standard IOL startup (no special handling needed)"
fi

# Clear IP addressing on eth0 (it 'belongs' to IOL now)
ip addr flush dev eth0
ip -6 addr flush dev eth0

echo "Flushed eth0 addresses"

sleep 5

# Run IOUYAP
exec /usr/bin/iouyap 513 -q &

# Get the highest numbered eth interface
max_eth=$(ls /sys/class/net | grep eth | grep -o -E "[0-9]+" | sort -n | tail -1)
num_slots=$(( (max_eth + 4) / 4 ))

# Start IOL
# -c config.txt: Works for 17.x, behavior varies for 15.x (some read it, some ignore)
# We keep both NVRAM and config.txt in sync for 15.x to handle all variants
exec /iol/iol.bin $IOL_PID -e $num_slots -s 0 -c config.txt -n 1024
