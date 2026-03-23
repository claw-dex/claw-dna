#!/bin/sh
# Check
ARCH=$(dpkg --print-architecture)
# if empty default to amd64
if [ -z "$ARCH" ]; then
  ARCH="amd64"
fi
if [ "$ARCH" = "amd64" ]; then
  ARCH="x86_64"
fi
if [ "$ARCH" = "arm64" ]; then
  ARCH="arm"
fi
# Install gcloud
echo "Starting install gcloud"
NAME="google-cloud-cli-linux-$ARCH"
URL="https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/$NAME.tar.gz"
INSTALL_DIR="/opt/google-cloud-sdk"
TMPDIR=$(mktemp -d)
TAR_FILE="$TMPDIR/$NAME.tar.gz"
echo "Downloading installer from $URL to $TMPDIR"
curl -fsSL -o "$TAR_FILE" "$URL"
# untar to temp dir
echo "Extracting installer"
tar -xzf "$TAR_FILE" -C "$TMPDIR"
# move SDK to permanent location before running installer
echo "Moving SDK to $INSTALL_DIR"
sudo mv "$TMPDIR/google-cloud-sdk" "$INSTALL_DIR"
# run installer
echo "Running installer"
"$INSTALL_DIR/install.sh" --quiet
echo "Cleaning up"
rm -rf "$TMPDIR"
echo "Install gcloud completed"
