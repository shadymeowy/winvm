# Windows in the VM

[Back to the README](../README.md)

- [What's different](#whats-different)
- [Activation](#activation)
- [Office](#office)
- [Turning updates back on](#turning-updates-back-on)
- [Changing what gets removed](#changing-what-gets-removed)

## What's different

**Removed or turned off:**

- Windows Update, Defender and SmartScreen
- Copilot, Recall and other AI features
- Edge and OneDrive, so there's no browser
- the Store and inbox apps
- telemetry and the search indexer
- hibernation, the pagefile and WinRE
- VBScript, so `.vbs` scripts such as `slmgr` don't run

The component store is also reset, and the payloads of disabled features are stripped, so
features that were removed can't simply be switched back on.

**Added:** virtio drivers, qemu-guest-agent, spice-vdagent (clipboard), and OpenSSH.

**Set on purpose:** the guest firewall is off, because everything reaches the guest through
ports bound to 127.0.0.1 on your host. The user `user` is an administrator with the password
`winslim`.

## Activation

The VM isn't activated. Windows is Microsoft software under Microsoft's license terms, and
activating it needs a license of your own. A license tied to your computer doesn't apply
by itself: the VM presents different hardware, so Windows doesn't see it.

To activate, run this in the guest (`winvm sh`) with your own key. The key must match the
image's edition, set by `edition:` in its file (Windows 11 Pro by default).

```powershell
$svc = Get-CimInstance SoftwareLicensingService
Invoke-CimMethod -InputObject $svc -MethodName InstallProductKey -Arguments @{ProductKey='XXXXX-XXXXX-XXXXX-XXXXX-XXXXX'}
Invoke-CimMethod -InputObject $svc -MethodName RefreshLicenseStatus | Out-Null
$win = "ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f' and PartialProductKey is not null"
Get-CimInstance SoftwareLicensingProduct -Filter $win | Invoke-CimMethod -MethodName Activate
(Get-CimInstance SoftwareLicensingProduct -Filter $win).LicenseStatus    # 1 = activated
```

**To keep it,** run `winvm save` afterwards. Every VM has the same SMBIOS UUID
(`settings.uuid`), so all images and instances look like one machine to activation, and
rebuilding or resetting keeps it.

**That is meant for one licensed machine.** Running several activated copies at the same time
needs a license for each. Give each its own settings file (`-f` or `$WINVM_CONFIG`) with its
own `uuid` and `data_dir`.

## Office

Office installs fine as an image step or by hand with the
[Office Deployment Tool](https://learn.microsoft.com/microsoft-365-apps/deploy/overview-office-deployment-tool),
which downloads from Microsoft's CDN, not Windows Update.

Microsoft 365 Apps need the WebView2 runtime and more than 4 GB of RAM without a pagefile. To get
the runtime, keep it with `debloat.remove_webview: false`, or run
`winget install Microsoft.EdgeWebView2Runtime` on the desktop or in a
[`desktop: true`](images.md#steps-on-a-logged-on-desktop) image. `winget` doesn't work from
`winvm sh` or a normal step.

## Turning updates back on

Updates are blocked by policy (a WSUS at 127.0.0.1) and by disabled services. To undo that:

1. Delete `HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate`.
2. Set `wuauserv`, `UsoSvc` and `WaaSMedicSvc` back to manual (`Start=3`).

## Changing what gets removed

The debloat lists ship inside the package. To change them, edit
`src/winvm/data/guest/lists/*.txt` in a checkout of <https://github.com/shadymeowy/winvm> and
install it with `uv tool install -e .`. `winvm build base` then installs with the new lists.
