# Signing Chatbox downloads

Release builds are **unsigned unless you add secrets**. A missing secret does not fail the build. The same workflow signs when the secrets below are present.

Certificates never go in the git repository. Add them as [Actions secrets](https://github.com/Artemmie/chatbox/settings/secrets/actions) on this repo.

## macOS

You need an Apple Developer Program membership.

1. In [Certificates](https://developer.apple.com/account/resources/certificates/list), create a **Developer ID Application** certificate. That is the one that signs apps distributed outside the Mac App Store.
2. Export it from Keychain Access as a `.p12` file, with a password you choose.
3. Base64-encode it: `base64 -i Certificates.p12 | pbcopy` (or `base64 -w0 Certificates.p12` on Linux).
4. Add these secrets:

| Secret | Value |
|---|---|
| `MACOS_CERT_P12_BASE64` | The base64 text of the `.p12` |
| `MACOS_CERT_PASSWORD` | The password you set when exporting |
| `MACOS_SIGN_IDENTITY` | The certificate name, for example `Developer ID Application: Your Name (TEAMID)` |

The workflow signs every Mach-O file inside `Chatbox.app` with the hardened runtime and `packaging/entitlements.plist`, then signs the disk image. It does not use `codesign --deep`.

### Notarization

Notarization is separate. Without these three secrets the app is signed and **not** notarized, and the build still succeeds.

1. At [appleid.apple.com](https://appleid.apple.com), create an app-specific password.
2. Your Team ID is the 10-character id on the membership page.

| Secret | Value |
|---|---|
| `APPLE_ID` | The Apple ID email |
| `APPLE_TEAM_ID` | The 10-character Team ID |
| `APPLE_APP_PASSWORD` | The app-specific password |

When all three are set, the workflow submits the app and the `.dmg` with `notarytool`, waits, and staples the tickets.

### First launch while unsigned or unnotarized

Gatekeeper blocks a normal double-click. Right-click `Chatbox.app` and choose **Open**, then Open again. Or, in Terminal:

```bash
xattr -dr com.apple.quarantine /Applications/Chatbox.app
```

The published Mac build is Apple silicon (`arm64`). Intel Macs can still use `./install.sh`.

## Windows

Use one of the two methods. Azure Trusted Signing is preferred when its secrets are complete. Otherwise a PFX is used. If neither is set, the `.exe` and the installer stay unsigned.

### Azure Trusted Signing

Create a Trusted Signing account and a certificate profile in the Azure portal, and an app registration that can sign. Then add:

| Secret | Value |
|---|---|
| `AZURE_TENANT_ID` | Directory (tenant) id |
| `AZURE_CLIENT_ID` | Application (client) id |
| `AZURE_CLIENT_SECRET` | A client secret for that app |
| `AZURE_TRUSTED_SIGNING_ENDPOINT` | The account endpoint, for example `https://eus.codesigning.azure.net` |
| `AZURE_TRUSTED_SIGNING_ACCOUNT` | Trusted Signing account name |
| `AZURE_TRUSTED_SIGNING_PROFILE` | Certificate profile name |

The release workflow calls `azure/trusted-signing-action` for the app folder and again for the installer, with an RFC 3161 timestamp.

### PFX

1. Obtain a code-signing certificate and export it as a `.pfx` with a password.
2. `base64 -w0 certificate.pfx` (Git Bash: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("certificate.pfx"))`).

| Secret | Value |
|---|---|
| `WINDOWS_CERT_PFX_BASE64` | The base64 text of the `.pfx` |
| `WINDOWS_CERT_PASSWORD` | The export password |

`packaging/sign-windows.ps1` runs `signtool` with SHA-256 and a DigiCert timestamp. This path is skipped when the Azure secrets above are all set.

### First launch while unsigned

SmartScreen shows "Windows protected your PC". Choose **More info**, then **Run anyway**.

## What the workflow refuses to do

It does not invent a certificate, and it does not fail the job when a secret is absent. A secret that is present but wrong (bad password, rejected notarization) fails that job, because the build claimed it could sign.
