# winslim: a minimal, reproducible Windows 11 VM

`winvm` turns an **official Microsoft Windows ISO** into a minimal Windows VM for
QEMU/KVM and libvirt. The build is unattended and reproducible, needs no root,
and is not activated (you activate it yourself).

* No Windows Update, Defender, SmartScreen, Copilot/Recall/AI, Edge, OneDrive,
  Store or inbox apps, telemetry, search indexer, hibernation or pagefile. WinRE
  is removed too.
* The component store is reset (ResetBase), payloads of disabled optional
  features are stripped, and Smart App Control is off (in evaluation mode it makes
  every signed installer wait 2 minutes once Defender is gone). The golden image
  is a zstd-compressed qcow2, and you run on a thin working disk, so a reset is
  instant. Installed software can be kept as named [layers](#layers-installing-software-keeping-it-rolling-back). Numbers are in [Results](#results).
* Guest tools are installed: virtio drivers (storage, net, balloon, serial, rng,
  input, gpu), **qemu-guest-agent** and spice-vdagent (clipboard in the GTK window).
* **Pure CLI mode.** Headless VM, OpenSSH server with key auth, PowerShell as the
  default shell. `./winvm ssh`, `./winvm run '<cmd>'` and `./winvm cp`. The guest
  agent still works when the network doesn't.
* **Samba share.** `$SHARE_DIR` on the host is `C:\host` in the guest (QEMU's
  built-in SMB server, no host config needed). It's read-write in both directions.
* **libvirt** domain (`qemu:///session`) with the same disk, ports and share.

Big files (ISOs, disks, caches) live in `~/Others/vm`. This repo holds only
scripts and config.

## Quick start

```sh
# 1. one-time: host packages (Fedora names)
sudo dnf install qemu-kvm edk2-ovmf xorriso p7zip p7zip-plugins wimlib-utils samba aria2
# optional: libvirt-daemon-kvm virt-manager (libvirt mode), guestfs-tools (offline cleanup), freerdp

# 2. Windows ISO: download in a browser from
#    https://www.microsoft.com/software-download/windows11  ("Download Windows 11 Disk Image (ISO)")
#    and save it as ~/Others/vm/iso/Win11_*.iso  (any official Win10/11 ISO works)

# 3. build (unattended)
./winvm fetch          # pinned virtio-win + OpenSSH, sha256-verified
./winvm editions       # optional: list editions, pick WIN_EDITION
./winvm install        # ~22 min: boot ISO + Microsoft ISO -> golden base image (6.2 GB)
#   or, recommended if you rebuild often / want the smallest image:
./winvm iso                     # ~1 h once: capture the debloated image -> 4.3 GB slim ISO
./winvm install --standalone    # ~6 min each time -> golden base image (5.4 GB)

# 4. use it
./winvm ssh            # starts the VM headless if needed, opens PowerShell
./winvm run 'Get-ComputerInfo | select OsName,OsBuildNumber'
./winvm cp ./tool.exe :C:/Users/user/   # ':' prefix = guest path
./winvm stop
```

## Commands

| command | what it does |
|---|---|
| `fetch` | download pinned virtio-win ISO and Win32-OpenSSH MSI, verify sha256 |
| `editions` | list editions inside the Windows ISO |
| `build` | build `winslim-boot.iso` (~650 MB): patched WinPE, scripts, drivers |
| `iso` | **iso→iso**: standalone installer `winslim-standalone.iso` holding the *already debloated* image |
| `iso-resume` | finish an interrupted `iso` from its kept capture disk |
| `install [--gui] [--standalone]` | unattended install, then golden `winslim-base.qcow2` and an empty working disk on it |
| `start [--gui]` | start headless (daemonized), or with a GTK window plus clipboard |
| `ssh`, `run`, `cp`, `wait` | CLI access over SSH (key auth, PowerShell) |
| `ga <cmd>` / `ga exec <prog> [args]` | qemu-guest-agent via socket, e.g. `./winvm ga exec ipconfig` |
| `rdp` | FreeRDP session (dynamic resolution, clipboard, host drive) |
| `gui` | GTK window, or a VNC viewer if already running headless |
| `stop` / `kill` / `status` | ACPI shutdown, hard off, overview |
| `reset [layer]` | discard the working disk: fresh empty one on the layer it was on, or on `[layer]` |
| `save <name>` | freeze the working disk as read-only layer `<name>`, continue on top of it |
| `layers` | list layers, sizes and what each is built on |
| `drop <name>` | delete a layer (refused while anything is built on it) |
| `balloon [size]` | show or change guest RAM live, returning memory to the host |
| `screenshot` | PNG of the guest screen, handy when headless |
| `cmdline` | print the exact QEMU command line used by `start` |
| `virt-define` / `virt-undefine` / `virsh …` | libvirt session domain |
| `clean [--base\|--all]` | remove build leftovers, also the base image, or everything |

## How it works

```
Win11.iso ─┬─ boot.wim (WinPE, index 2) ─ wimlib: winpeshl.ini → winslim-start.cmd
           │                                   │
           │          winslim-boot.iso = WinPE + winslim/ (pe.cmd, lists, unattend,
           │                              payload) + drivers/ (virtio w11/amd64)
           │
           └─ sources/install.wim ◄──── read by pe.cmd from the 2nd CD-ROM
```

1. **WinPE stage** (`guest/pe.cmd`, replaces setup.exe). It refuses to run unless
   the SMBIOS vendor is QEMU. Then it loads vioscsi, partitions disk 0 (ESP 100 MB
   + MSR + C:, no recovery partition) and runs `dism /Apply-Image` (with
   `/Compact` if `WIN_COMPACT=1`). From
   there it uses the applied image's own DISM, because WinPE's is older and lacks
   the AppX provider. It removes provisioned AppX packages not listed in
   `appx-keep.txt`, the capabilities in `capabilities-remove.txt` and the features
   in `features-remove.txt`, strips the payloads of disabled features, and runs
   `/Cleanup-Image /ResetBase`. Next come the offline registry edits
   (`offline-reg.txt`), disabling services (`services-disable.txt`) and deleting
   Edge, OneDrive setup and WinRE (`offline-delete.txt`). Finally it injects the
   virtio drivers, stages `unattend.xml` and the first-boot scripts, and runs
   bcdboot.
2. **specialize/OOBE**: fully unattended, local admin account, no network
   (`restrict=on`), no TPM or Secure Boot needed.
3. **SetupComplete** (`setupcomplete.ps1`, SYSTEM): installs the virtio guest tools
   and the OpenSSH server (with your keys), turns the firewall off, creates the
   `C:\host` link and sets power settings. It repeats the debloat sweep online,
   disables scheduled tasks, runs ResetBase (plus CompactOS if `WIN_COMPACT=1`),
   and re-trims.
4. **First logon** (`firstlogon.ps1`): a single autologon creates the user profile
   inside the base image, removes the Edge WebView2 runtime (after the user
   OOBE, which may still need it), clears the autologon secrets and powers off.
5. Host: libguestfs, if installed, deletes the few WebView2 files the logged-in
   shell kept locked, then fstrims. `qemu-img convert -c` (zstd) gives the
   read-only **base**, plus a thin read-write **working disk** that you actually run.

Progress streams over the serial port (`~/Others/vm/run/winslim/install-serial.log`).
Logs inside the guest are in `C:\Windows\Setup\winslim*.log`.

### iso → iso (`./winvm iso`)

`./winvm install` needs two CDs: the small boot ISO plus Microsoft's ISO. `./winvm iso`
instead produces one self-contained installer that holds the already-slimmed
image. It boots the boot ISO in capture mode, where WinPE applies and services
the image exactly as above. Then, before the first boot, it runs
`dism /Capture-Image`, writing the serviced `C:` to a spare partition, and
powers off. The host pulls `install.wim` out with libguestfs, re-exports it with
wimlib as a solid LZMS `install.esd` (WinPE runs out of memory doing that), and
wraps it with the WinPE tree. The installer
on that ISO skips servicing (it's already done) and only applies, stages the
first-boot scripts and boots. `./winvm install --standalone` uses it.

## Customizing

Put overrides in `config.local.env` (git-ignored). Example:

```sh
WIN_EDITION="Windows 11 Pro N"    # N editions are smaller (no media stack)
WIN_PASSWORD='s3cret'
WIN_KEYBOARD=041f:0000041f        # Turkish Q
RAM=8G CPUS=8 SSH_PORT=2022
SHARE_DIR=$HOME/Projects
WIN_REMOVE_WEBVIEW=0              # keep Edge WebView2 (some apps need it)
WIN_COMPACT=1                     # CompactOS inside the guest instead (see Results)
WIMLIB_THREADS=2                  # `iso` export threads; solid LZMS needs ~1 GiB RAM each
MEM_RESERVE=1024                  # MiB of host RAM that must stay free
```

**Host RAM guard.** Before every VM start (`install`, `iso`, `start`) and every
wimlib export, `winvm` checks `MemAvailable`. It refuses to start when the job
plus `MEM_RESERVE` won't fit, instead of pushing the host into swap or the OOM
killer. Lower `RAM` or `WIMLIB_THREADS` if you hit it.

Debloat is data-driven. Edit `guest/lists/*.txt`, then `./winvm clean --base && ./winvm install`.
`WINSLIM_DEBUG=1 ./winvm build` makes a WinPE failure drop to a shell (watch it
with VNC or `./winvm screenshot`) instead of powering off.

## Access details

* SSH: `127.0.0.1:2222`, user `user`. The key is `~/Others/vm/keys/id_ed25519`,
  and `~/.ssh/id_ed25519.pub` is also authorized. To use plain `ssh winslim`, or
  VS Code Remote-SSH, add `Include ~/Others/vm/run/winslim/ssh_config` to `~/.ssh/config`.
* RDP: `127.0.0.1:3390`, user `user`, password `winslim` (`./winvm rdp`).
* VNC (headless console): `127.0.0.1:5930`.
* Share: host `~/Others/vm/share` ⇄ guest `C:\host` = `\\10.0.2.4\qemu`. This
  needs `smbd` on the host; QEMU starts its own private instance.
* All forwards bind to 127.0.0.1 only. The guest firewall is off on purpose.

## libvirt

```sh
./winvm virt-define                 # renders libvirt/winslim.xml.in into qemu:///session
./winvm virsh start winslim
virt-manager -c qemu:///session     # GUI, if you like
./winvm ssh                         # same ports/share as the plain QEMU launcher
```

Don't run the libvirt domain and `./winvm start` at the same time. They share the
working disk, and QEMU's image locking will refuse.

**SMB share under libvirt:** libvirt starts QEMU with a seccomp sandbox that
forbids spawning processes, so QEMU can't launch its private `smbd`, and the
sandbox can't be relaxed per domain. SSH, RDP, the guest agent and internet all
work regardless. To get `C:\host` under libvirt too, turn the sandbox off for
your session daemon (`virt-define` warns until you do):

```sh
echo 'seccomp_sandbox = 0' >> ~/.config/libvirt/qemu.conf
systemctl --user restart virtqemud 2>/dev/null || pkill -u "$USER" virtqemud
```

The domain runs with `<seclabel type='none'/>` (unconfined, like the plain
launcher), so QEMU can read the share in your home directory despite SELinux.

## Results

Measured on this repo's defaults (Windows 11 Pro, build 26300, 4 vCPU/4 GB, KVM):

| | |
|---|---|
| `install` (Microsoft ISO → golden image) | ~22 min |
| `iso` (Microsoft ISO → slim standalone ISO, 2 wimlib threads) | ~25 min VM + ~35 min export |
| slim standalone ISO (Microsoft's is 9.0 GB) | **4.3 GB** |
| `install --standalone` (slim ISO → golden image) | **~6 min** |
| boot to SSH prompt | ~12 s |
| graceful shutdown | ~7 s |
| golden base image, from the slim ISO | **5.4 GB** |
| golden base image, from Microsoft's ISO (`WIN_COMPACT=0`, zstd qcow2) | 6.2 GB |
| same with CompactOS in the guest (`WIN_COMPACT=1`) | 7.5 GB |
| fresh working disk | < 1 MB |
| idle RAM in the guest (headless, nobody logged in) | ~950 MB, 86 processes |
| host RSS of a 4 GB VM after `./winvm balloon 2G` | 4.2 GB → 2.1 GB |

XPRESS4K (CompactOS) only reaches about 1.8:1, and its output is incompressible
for qcow2. Leaving the guest data raw and letting qcow2's zstd compress it gives
the smaller host footprint, so that's the default. Blocks you write later land
uncompressed in the working disk (and the layers you `save`); `./winvm reset` gets
you back to the layer below.

## Layers (installing software, keeping it, rolling back)

Disks are plain qcow2 files in `~/Others/vm/images`. Nothing else holds state:

```
winslim-base.qcow2     read-only   golden image from `install`          (layer "base")
winslim-office.qcow2   read-only   a layer you saved, backed by base
winslim-dev.qcow2      read-only   another one, backed by office
winslim.qcow2          read-write  the working disk the VM boots, backed by one layer
```

Each file only stores the blocks that differ from the file under it. Backing files
are referenced by bare name, so `qemu-img info --backing-chain winslim.qcow2`
shows exactly the same thing as `./winvm layers`.

```sh
./winvm ssh                # install things; everything lands in winslim.qcow2
./winvm stop
./winvm save office        # winslim.qcow2 -> winslim-office.qcow2 (read-only), new empty working disk on it
./winvm reset              # throw away what you did since: back to "office"
./winvm reset base         # clean Windows again (office is kept)
./winvm reset office       # and back
./winvm layers             # base 5.4G | office 3.1G <- base | working 2M <- office
./winvm drop office        # delete it (refused while something is built on it)
```

Saved layers are never written again, so whatever is built on them stays valid.
Only one working disk exists at a time. Activate Windows/Office in a layer you then
`save`, otherwise `reset` throws the activation away. The VM has a fixed SMBIOS UUID
(`VM_UUID`, same in the libvirt domain) so every layer looks like the same machine.

### Office

Office installs fine into a layer. Use the [Office Deployment Tool][odt]. It
downloads from Microsoft's Office CDN, not Windows Update.

```powershell
winget install Microsoft.EdgeWebView2Runtime   # Microsoft 365 Apps need it (sign-in, add-ins); removed by winslim
winget install Microsoft.OfficeDeploymentTool  # or download setup.exe by hand
# config.xml: pick a product (O365ProPlusRetail, ProPlus2024Volume, ...), one language,
# exclude what you don't want and freeze updates like the rest of the VM:
#   <Add OfficeClientEdition="64" Channel="Current"><Product ID="O365ProPlusRetail">
#     <Language ID="en-us"/><ExcludeApp ID="Teams"/><ExcludeApp ID="OneDrive"/>
#     <ExcludeApp ID="Outlook"/><ExcludeApp ID="Groove"/><ExcludeApp ID="Lync"/></Product></Add>
#   <Updates Enabled="FALSE"/><Display Level="None" AcceptEULA="TRUE"/>
& "C:\Program Files\OfficeDeploymentTool\setup.exe" /configure config.xml
```

Then `./winvm stop && ./winvm save office`, activate, stop, `./winvm save office-activated`.
Expect 3–4 GB. Office wants more than 4 GB RAM with no pagefile, so start it with
`RAM=6G ./winvm start` (config.local.env to make it stick).

[odt]: https://learn.microsoft.com/microsoft-365-apps/deploy/overview-office-deployment-tool

## Activation and re-enabling things

The VM is unactivated (do it in a layer you `save`, see above). Run `slmgr /ipk <your-key>` then `slmgr /ato`, or use
Settings › Activation. Updates are blocked by policy (a WSUS pointing to
127.0.0.1) plus disabled services. To undo: delete
`HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate`, and set `wuauserv`,
`UsoSvc` and `WaaSMedicSvc` back to manual (Start=3).

## Notes

* The standalone ISO from `./winvm iso` wipes disk 0 without asking. It refuses
  to run unless the machine reports SMBIOS vendor QEMU (create `winslim\force`
  on the media to override).
* The Windows ISO has to be downloaded by hand. `winvm` doesn't script
  Microsoft's download site.
