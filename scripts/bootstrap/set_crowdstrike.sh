#!/bin/bash
# ==============================================================================
# Install and Configure Crowdstrike (Non-interactive ready)
# ==============================================================================
set -uo pipefail

# 1. check root or not
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root."
    exit 1
fi

SERVICE_NAME="falcon-sensor"
CS_INSTALL_TYPE="${CS_INSTALL_TYPE:-NGAV}"
MDR_CID="${MDR_CID:-}"
MDR_TOKEN="${MDR_TOKEN:-}"
NGAV_CID="${NGAV_CID:-}"
NGAV_TOKEN="${NGAV_TOKEN:-}"
CS_PROXY_HOST="${CS_PROXY_HOST:-}"
CS_PROXY_PORT="${CS_PROXY_PORT:-}"

echo "=== Starting Crowdstrike Installation Workflow (Mode: $CS_INSTALL_TYPE) ==="

# 2. Identify Linux distribution
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_FAMILY="${ID:-}"
    OS_LIKE="${ID_LIKE:-}"
    OS_VERSION="${VERSION_ID:-}"
else
    echo "ERROR: Cannot determine OS distribution via /etc/os-release."
    exit 2
fi

echo "Detected OS ID: $OS_FAMILY, Like: $OS_LIKE, Version: $OS_VERSION"

INSTALLER_PATH=""
if [[ "$OS_FAMILY" == "ubuntu" || "$OS_FAMILY" == "debian" || "$OS_LIKE" == *"debian"* || "$OS_LIKE" == *"ubuntu"* ]]; then
    PACKAGE_TYPE="deb"
    INSTALLER_PATH=$(ls /tmp/CrowdStrike/Ubuntu/falcon-sensor*.deb 2>/dev/null | head -n 1 || true)
    INSTALLER_PATH="${INSTALLER_PATH:-/tmp/CrowdStrike/Ubuntu/falcon-sensor_7.32.0-18504_amd64.deb}"

elif [[ "$OS_FAMILY" == "centos" || "$OS_FAMILY" == "rhel" || "$OS_FAMILY" == "rocky" || "$OS_FAMILY" == "oracle" || "$OS_FAMILY" == "ol" || "$OS_FAMILY" == "almalinux" || "$OS_LIKE" == *"rhel"* || "$OS_LIKE" == *"fedora"* ]]; then
    PACKAGE_TYPE="rpm"
    if [[ "$OS_VERSION" == "8"* ]]; then
        INSTALLER_PATH=$(ls /tmp/CrowdStrike/RHEL_8/falcon-sensor*.rpm 2>/dev/null | head -n 1 || true)
        INSTALLER_PATH="${INSTALLER_PATH:-/tmp/CrowdStrike/RHEL_8/falcon-sensor-7.35.0-18803.el8.x86_64.rpm}"
    elif [[ "$OS_VERSION" == "9"* ]]; then
        INSTALLER_PATH=$(ls /tmp/CrowdStrike/RHEL_9/falcon-sensor*.rpm 2>/dev/null | head -n 1 || true)
        INSTALLER_PATH="${INSTALLER_PATH:-/tmp/CrowdStrike/RHEL_9/falcon-sensor-7.35.0-18803.el9.x86_64.rpm}"
    else
        echo "ERROR: Unsupported RHEL/CentOS version ($OS_VERSION)."
        exit 5
    fi
else
    echo "ERROR: Unsupported Linux distribution family ($OS_FAMILY)."
    exit 5
fi

if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "[OK] Crowdstrike ($SERVICE_NAME) is already running, skipping installation."
    exit 0
fi

if [ ! -f "$INSTALLER_PATH" ]; then
    echo "ERROR: Installer package $INSTALLER_PATH not found."
    exit 3
fi

# 3. Install using proper operations
if [ "$PACKAGE_TYPE" == "deb" ]; then
    echo "Installing DEB package ($INSTALLER_PATH) via dpkg..."
    export DEBIAN_FRONTEND=noninteractive
    if ! dpkg -i "$INSTALLER_PATH" > /tmp/crowdstrike_install.log 2>&1; then
        echo "ERROR: DEB installation failure. See /tmp/crowdstrike_install.log"
        tail -n 20 /tmp/crowdstrike_install.log || true
        exit 4
    fi
elif [ "$PACKAGE_TYPE" == "rpm" ]; then
    echo "Installing RPM package ($INSTALLER_PATH) via yum..."
    if ! yum install -y "$INSTALLER_PATH" > /tmp/crowdstrike_install.log 2>&1; then
        echo "ERROR: RPM installation failed. See /tmp/crowdstrike_install.log"
        tail -n 20 /tmp/crowdstrike_install.log || true
        exit 4
    fi
fi

echo "Installation command executed successfully."

# 4. setup parameters
if [[ "$CS_INSTALL_TYPE" == "m" || "$CS_INSTALL_TYPE" == "M" || "$CS_INSTALL_TYPE" == "MDR" ]]; then
    echo "Setting up Crowdstrike for full MDR..."
    /opt/CrowdStrike/falconctl -s --cid="$MDR_CID" --provisioning-token="$MDR_TOKEN" --cloud=us-2
else
    echo "Setting up Crowdstrike for default NGAV..."
    /opt/CrowdStrike/falconctl -s --cid="$NGAV_CID" --provisioning-token="$NGAV_TOKEN"
fi

systemctl start falcon-sensor
/opt/CrowdStrike/falconctl -s --aph="$CS_PROXY_HOST" --app="$CS_PROXY_PORT"
/opt/CrowdStrike/falconctl -g --aph --app
/opt/CrowdStrike/falconctl -s --apd=FALSE

# 5. check service status
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "[OK] falcon-sensor service is active and running."
else
    echo "ERROR: falcon-sensor failed to start."
    exit 6
fi
