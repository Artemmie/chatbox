#!/bin/bash
# Sign when Developer ID secrets are set. Unsigned builds continue.
#   packaging/sign-macos.sh app    codesign dist/Chatbox.app, notarize it when Apple ID secrets exist
#   packaging/sign-macos.sh dmg    codesign and notarize dist/artifacts/Chatbox-macOS-arm64.dmg
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-app}"
APP="dist/Chatbox.app"
DMG="dist/artifacts/Chatbox-macOS-arm64.dmg"

decode_p12() {
  if printf '%s' "$MACOS_CERT_P12_BASE64" | base64 -D > "$1" 2>/dev/null && [ -s "$1" ]; then
    return 0
  fi
  printf '%s' "$MACOS_CERT_P12_BASE64" | base64 -d > "$1"
}

if [ -z "${MACOS_CERT_P12_BASE64:-}" ] || [ -z "${MACOS_CERT_PASSWORD:-}" ] || [ -z "${MACOS_SIGN_IDENTITY:-}" ]; then
  echo "No Mac signing secrets. Leaving the ${MODE} unsigned."
  exit 0
fi
if [ "$MODE" = "dmg" ]; then
  if [ ! -f "$DMG" ]; then
    echo "Missing $DMG" >&2
    exit 1
  fi
fi
if [ "$MODE" = "app" ] && [ ! -d "$APP" ]; then
  echo "Missing $APP" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
decode_p12 "$WORKDIR/cert.p12"
security create-keychain -p temp "$WORKDIR/build.keychain"
security default-keychain -s "$WORKDIR/build.keychain"
security unlock-keychain -p temp "$WORKDIR/build.keychain"
security set-keychain-settings -lut 21600 "$WORKDIR/build.keychain"
security import "$WORKDIR/cert.p12" -k "$WORKDIR/build.keychain" -P "$MACOS_CERT_PASSWORD" -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k temp "$WORKDIR/build.keychain" >/dev/null

sign_one() {
  codesign --force --options runtime --timestamp \
    --entitlements packaging/entitlements.plist \
    --sign "$MACOS_SIGN_IDENTITY" "$1"
}

if [ "$MODE" = "dmg" ]; then
  codesign --force --timestamp --sign "$MACOS_SIGN_IDENTITY" "$DMG"
  if [ -z "${APPLE_ID:-}" ] || [ -z "${APPLE_TEAM_ID:-}" ] || [ -z "${APPLE_APP_PASSWORD:-}" ]; then
    echo "Signed the disk image. Notarization secrets are not set."
    exit 0
  fi
  xcrun notarytool submit "$DMG" \
    --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD" --wait
  xcrun stapler staple "$DMG"
  echo "Notarized and stapled $DMG"
  exit 0
fi

# Sign nested Mach-O files first, then the bundle. --deep is not used.
while IFS= read -r -d '' path; do
  if file "$path" | grep -q "Mach-O"; then
    sign_one "$path"
  fi
done < <(find "$APP" -type f -print0)
sign_one "$APP"
codesign --verify --verbose=2 "$APP"
echo "Signed $APP as $MACOS_SIGN_IDENTITY"

if [ -z "${APPLE_ID:-}" ] || [ -z "${APPLE_TEAM_ID:-}" ] || [ -z "${APPLE_APP_PASSWORD:-}" ]; then
  echo "Notarization secrets are not set. The app is signed and not notarized."
  exit 0
fi

# Submit a zip, then staple the app itself (stapler does not staple a zip).
DITTO_ZIP="$WORKDIR/Chatbox-notarize.zip"
ditto -c -k --keepParent "$APP" "$DITTO_ZIP"
xcrun notarytool submit "$DITTO_ZIP" \
  --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD" --wait
xcrun stapler staple "$APP"
echo "Notarized and stapled $APP"
