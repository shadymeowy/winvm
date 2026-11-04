# winslim: a minimal, reproducible Windows 11 VM

`winvm` turns an **official Microsoft Windows ISO** into a minimal Windows VM for
QEMU/KVM. The build is unattended and reproducible, needs no root, and is not
activated (you activate it yourself). `winvm` is a single Python 3 file with no
dependencies beyond the standard library.

* No Windows Update, Defender, SmartScreen, Copilot/Recall/AI, Edge, OneDrive,
  Store or inbox apps, telemetry, search indexer, hibernation or pagefile. WinRE
  is removed too.
* The component store is reset (ResetBase), payloads of disabled optional
  features are stripped, and Smart App Control is off (in evaluation mode it makes
  every signed installer wait 2 minutes once Defender is gone). The golden image
  is a zstd-compressed qcow2 **layer**. Instances run on thin disks on top of it,
  so a reset is instant, and installed software can be kept as further layers.
  Numbers are in [Results](#results).
* Guest tools are installed: virtio drivers (storage, net, balloon, serial, rng,
  input, gpu), **qemu-guest-agent** and spice-vdagent (clipboard in the GTK window).
* **Pure CLI mode.** Headless VM, OpenSSH server with key auth, PowerShell as the
  default shell. `./winvm sh <command>` runs a guest command like a local one:
  exit code, stdin, pipes, Unix line endings, and the current folder when you're
  inside the share. About 0.6 s per command.
* **Samba share.** `$SHARE_DIR` on the host is `C:\host` in the guest (QEMU's
  built-in SMB server, no host config needed). It's read-write in both directions.
* **Several instances at once**, each with its own disk and ports.

Big files (ISOs, disks, caches) live in `~/Others/vm`. This repo holds only
scripts and config.

## Quick start

```sh
# 1. one-time: host packages (Fedora names)
sudo dnf install qemu-kvm edk2-ovmf xorriso p7zip p7zip-plugins wimlib-utils samba aria2
# optional: guestfs-tools (offline cleanup, needed by `iso`), freerdp

# 2. Windows ISO: download in a browser from
#    https://www.microsoft.com/software-download/windows11  ("Download Windows 11 Disk Image (ISO)")
#    and save it as ~/Others/vm/iso/Win11_*.iso  (any official Win10/11 ISO works)

# 3. build (unattended)
./winvm install                 # ~22 min: boot ISO + Microsoft ISO -> layer "base"
#   or, recommended if you rebuild often / want the smallest image:
./winvm iso                     # ~1 h once: capture the debloated image -> 4.2 GB slim ISO
./winvm install --standalone    # ~10 min each time -> layer "base"

# 4. use it
./winvm sh                      # interactive PowerShell (starts the VM if needed)
./winvm sh Get-ComputerInfo -Property OsName,OsBuildNumber
./winvm cp ./tool.exe :C:/Users/user/    # ':' prefix = guest path
./winvm stop
```

## Running commands in the guest: `winvm sh`

```sh
./winvm sh                          # interactive shell
./winvm sh ipconfig /all            # a command line; PowerShell parses it
./winvm sh 'gci C:\ | select Name'  # one quoted argument = PowerShell code as is
./winvm sh notepad "a b.txt"        # words with spaces stay one argument
./winvm sh -t python                # -t: a terminal, for interactive programs
cat x | ./winvm sh findstr foo      # stdin reaches native programs ...
cat x | ./winvm sh 'stdin | sort'   # ... and PowerShell code reads it with `stdin`
./winvm sh ipconfig | grep IPv4     # output has Unix line endings
./winvm sh cmd /c exit 3; echo $?   # 3: the exit code is the command's (1 for a failed cmdlet)
cd ~/Others/vm/share/proj && ~/Projects/vm/winvm sh dir   # runs in C:\host\proj
```

The command is shipped base64-encoded, so no quoting is lost between your shell,
ssh, sshd and Windows. All commands to an instance share one SSH connection.
Handy alias: `alias w='~/Projects/vm/winvm sh'`, then `w ipconfig`.

`./winvm ssh [options]` is plain ssh (port forwards, `-v`, ...).
`./winvm ga exec <prog> [args]` goes through the guest agent and works without network.

## Commands

| command | what it does |
|---|---|
| `fetch` | download the pinned virtio-win ISO and Win32-OpenSSH MSI, verify sha256 |
| `editions` | list the editions inside the Windows ISO |
| `build` | build `winslim-boot.iso` (~650 MB): patched WinPE, scripts, drivers |
| `iso` | **iso→iso**: standalone installer `winslim-standalone.iso` holding the *already debloated* image |
| `iso --payload` | renew only the first-boot scripts, extras and keys in that ISO, keeping its image (~20 s) |
| `iso-resume` | finish an interrupted `iso` from its kept capture disk |
| `install [--standalone] [--gui]` | unattended install → read-only layer `base`, plus a disk for instance `main` |
| `start [--gui] [--from LAYER]` | start headless, or with a GTK window plus clipboard |
| `stop` / `kill` | ACPI shutdown (hard kill after 120 s) / hard power-off |
| `ps` | instances: state, ports, disk size, layer chain |
| `sh [-t] [CMD]` | PowerShell in the guest, see above |
| `ssh`, `cp`, `wait` | plain ssh, scp (`:` = guest path), wait for SSH |
| `rdp` / `gui` | FreeRDP session (resolution, clipboard, share) / VNC viewer or GTK window |
| `ga [CMD] / ga exec PROG [ARGS]` | qemu-guest-agent, e.g. `./winvm ga guest-info` |
| `balloon [SIZE]` | show or change guest RAM live, returning memory to the host |
| `screenshot [FILE]` | PNG of the guest screen, handy when headless |
| `cmdline` | print the exact QEMU command line `start` uses |
| `layers` | layers, sizes, what each is built on and who uses it |
| `save NAME` / `reset [LAYER]` | freeze the disk as a layer / discard changes |
| `drop NAME` / `rm` | delete a layer / delete an instance |
| `clean [--base] [--all]` | remove build leftovers, also the base layer, or everything |

Every command acts on instance `main` unless you put `-i NAME` before it
(`./winvm -i dev sh`), or set `INSTANCE` in the environment or `config.local.env`.

## Layers and instances

Disks are plain qcow2 files in `~/Others/vm/images`. Nothing else holds state:

```
layers/base.qcow2     read-only   golden image from `install`
layers/office.qcow2   read-only   a layer you saved, backed by base
disks/main.qcow2      read-write  instance "main", backed by one layer
disks/dev.qcow2       read-write  instance "dev", backed by another (or the same) layer
```

Each file stores only the blocks that differ from the file below it. Backing files
are referenced by relative path, so `qemu-img info --backing-chain disks/main.qcow2`
shows the same as `./winvm layers`, and `~/Others/vm` can be moved.

```sh
./winvm sh                     # install things; everything lands in disks/main.qcow2
./winvm stop
./winvm save office            # disks/main -> layers/office (read-only); main continues on top of it
./winvm reset                  # throw away what you did since: back to "office"
./winvm reset base             # clean Windows again (office is kept)
./winvm -i scratch start --from office   # a second instance on the same layer, at the same time
./winvm ps                     # main 2222/3390/5930, scratch 2223/3391/5931, ...
./winvm -i scratch rm          # delete that instance
./winvm drop office            # delete the layer (refused while anything is built on it)
```

Saved layers are never written again, so whatever is built on them stays valid.
A starting instance takes the lowest free port slot *n* (preferring the one it had
last time): `SSH_PORT`+*n*, `RDP_PORT`+*n*, `VNC_DISPLAY`+*n*. The first one gets
the plain ports. `./winvm ps` shows them. The host RAM guard (below) decides how many instances
fit. Activate Windows/Office in a layer you then `save`, otherwise `reset` throws
the activation away. Every instance has the same SMBIOS UUID (`VM_UUID`), so all
layers look like the same machine to activation.

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
`RAM=6G ./winvm start` (or set `RAM` in `config.local.env`).

[odt]: https://learn.microsoft.com/microsoft-365-apps/deploy/overview-office-deployment-tool

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
   `/Compact` if `WIN_COMPACT=1`). From there it uses the applied image's own DISM,
   because WinPE's is older and lacks the AppX provider. It removes provisioned AppX
   packages not listed in `appx-keep.txt`, the capabilities in
   `capabilities-remove.txt` and the features in `features-remove.txt`, strips the
   payloads of disabled features, and runs `/Cleanup-Image /ResetBase`. Next come
   the offline registry edits (`offline-reg.txt`), disabling services
   (`services-disable.txt`) and deleting Edge, OneDrive setup and WinRE
   (`offline-delete.txt`). Finally it injects the virtio drivers, stages
   `unattend.xml` and the first-boot scripts, and runs bcdboot.
2. **specialize/OOBE**: fully unattended, local admin account, no network
   (the installer VM has no NIC), no TPM or Secure Boot needed.
3. **SetupComplete** (`setupcomplete.ps1`, SYSTEM): installs the virtio guest tools
   and the OpenSSH server (with your keys), compiles .NET native images (PowerShell
   starts in 0.2 s instead of 2 s), turns the firewall off, creates the `C:\host`
   link and sets power settings. It repeats the debloat sweep online, disables
   scheduled tasks, runs ResetBase (plus CompactOS if `WIN_COMPACT=1`), and re-trims.
4. **First logon** (`firstlogon.ps1`): a single autologon creates the user profile
   inside the base image, removes the Edge WebView2 runtime (after the user
   OOBE, which may still need it), clears the autologon secrets and powers off.
5. Host: libguestfs, if installed, deletes the few WebView2 files the logged-in
   shell kept locked, then fstrims. `qemu-img convert -c` (zstd) gives the
   read-only layer `base`, and instance `main` gets an empty disk on top of it.

Progress streams over the serial port (`~/Others/vm/run/_install/install-serial.log`).
Logs inside the guest are in `C:\Windows\Setup\winslim*.log`.

### iso → iso (`./winvm iso`)

`./winvm install` needs two CDs: the small boot ISO plus Microsoft's ISO. `./winvm iso`
instead produces one self-contained installer that holds the already-slimmed
image. It boots the boot ISO in capture mode, where WinPE applies and services
the image exactly as above. Then, before the first boot, it runs
`dism /Capture-Image`, writing the serviced `C:` to a spare partition, and
powers off. The host pulls `install.wim` out with libguestfs, re-exports it with
wimlib as a solid LZMS `install.esd` (WinPE runs out of memory doing that), and
wraps it with the WinPE tree. The installer on that ISO skips servicing (it's
already done) and only applies, stages the first-boot scripts and boots.
`./winvm install --standalone` uses it.

## Customizing

`config.env` holds the defaults as plain `KEY=value` lines. Put overrides in
`config.local.env` (git-ignored), or pass them as environment variables
(`RAM=8G ./winvm start`). Example `config.local.env`:

```sh
WIN_EDITION=Windows 11 Pro N      # N editions are smaller (no media stack)
WIN_PASSWORD=s3cret
WIN_KEYBOARD=041f:0000041f        # Turkish Q
RAM=8G
CPUS=8
SHARE_DIR=$HOME/Projects
WIN_REMOVE_WEBVIEW=0              # keep Edge WebView2 (some apps need it)
WIN_COMPACT=1                     # CompactOS inside the guest instead (see Results)
WIMLIB_THREADS=2                  # `iso` export threads; solid LZMS needs ~1 GiB RAM each
MEM_RESERVE=1024                  # MiB of host RAM that must stay free
```

**Host RAM guard.** Before every VM start (`install`, `iso`, `start`) and every
wimlib export, `winvm` checks `MemAvailable`, minus the RAM that running instances
may still grow into. It refuses when the job plus `MEM_RESERVE` won't fit,
instead of pushing the host into swap or the OOM killer. The `iso` export also
refuses to run next to any VM.

### Adding things to the base

For software you try out or change often, install it in an instance and
`./winvm save` it as a layer (see above). To bake something into every base build,
put its installer and a `*.ps1` that runs it into **`guest/extra/`** (details in
[guest/extra/README.md](guest/extra/README.md)). Setup runs those scripts as
SYSTEM, offline, after the debloat. What to rerun after a change:

| you changed | Microsoft ISO path | slim ISO path |
|---|---|---|
| `guest/extra/`, `guest/payload/`, user/password/keys in the config | `./winvm build` | `./winvm iso --payload` (~20 s) |
| the image itself: `guest/lists/*.txt`, `guest/pe.cmd`, `WIN_EDITION`, `WIN_STRIP_PAYLOADS` | `./winvm build` | `./winvm iso` (~1 h, new capture) |

then `./winvm clean --base && ./winvm install [--standalone]`.

`WINSLIM_DEBUG=1 ./winvm build` makes a WinPE failure drop to a shell (watch it
with VNC or `./winvm screenshot`) instead of powering off.

## Access details

* SSH: `127.0.0.1:2222` (+*n* for further instances), user `user`. The key is
  `~/Others/vm/keys/id_ed25519`, and `~/.ssh/id_ed25519.pub` is also authorized.
  To use plain `ssh main`, or VS Code Remote-SSH, add
  `Include ~/Others/vm/run/*/ssh_config` to `~/.ssh/config`.
* RDP: `127.0.0.1:3390`, user `user`, password `winslim` (`./winvm rdp`).
* VNC (headless console): `127.0.0.1:5930`.
* Share: host `~/Others/vm/share` ⇄ guest `C:\host` = `\\10.0.2.4\qemu`. This
  needs `smbd` on the host; QEMU starts its own private instance.
* All forwards bind to 127.0.0.1 only. The guest firewall is off on purpose.

## Results

Measured on this repo's defaults (Windows 11 Pro, build 26300, 4 vCPU/4 GB, KVM):

| | |
|---|---|
| `install` (Microsoft ISO → base layer) | ~22 min |
| `iso` (Microsoft ISO → slim standalone ISO, 2 wimlib threads) | ~30 min VM + ~30 min export |
| slim standalone ISO (Microsoft's is 9.0 GB) | **4.2 GB** |
| `install --standalone` (slim ISO → base layer, incl. compression) | **~10 min** |
| boot to SSH prompt | ~12 s |
| `./winvm sh hostname` (connection open) | **~0.6 s** |
| graceful shutdown | ~7 s |
| base layer, from the slim ISO (incl. ~0.2 GB .NET native images) | **5.5 GB** |
| base layer, from Microsoft's ISO (`WIN_COMPACT=0`, zstd qcow2) | ~6.4 GB |
| same with CompactOS in the guest (`WIN_COMPACT=1`) | ~7.7 GB |
| fresh instance disk | < 1 MB |
| idle RAM in the guest (headless, nobody logged in) | ~950 MB, 86 processes |
| host RSS of a 4 GB VM after `./winvm balloon 2G` | 4.2 GB → 2.1 GB |

XPRESS4K (CompactOS) only reaches about 1.8:1, and its output is incompressible
for qcow2. Leaving the guest data raw and letting qcow2's zstd compress it gives
the smaller host footprint, so that's the default. Blocks you write later land
uncompressed in the instance disk (and the layers you `save`); `./winvm reset`
gets you back to the layer below.

## Activation and re-enabling things

The VM is unactivated (do it in a layer you `save`, see above). Run
`slmgr /ipk <your-key>` then `slmgr /ato`, or use Settings › Activation. Updates
are blocked by policy (a WSUS pointing to 127.0.0.1) plus disabled services. To
undo: delete `HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate`, and set
`wuauserv`, `UsoSvc` and `WaaSMedicSvc` back to manual (Start=3).

## Notes

* The standalone ISO from `./winvm iso` wipes disk 0 without asking. It refuses
  to run unless the machine reports SMBIOS vendor QEMU (create `winslim\force`
  on the media to override).
* The Windows ISO has to be downloaded by hand. `winvm` doesn't script
  Microsoft's download site.
