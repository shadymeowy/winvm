# winslim: a minimal, reproducible Windows 11 VM

`winvm` turns an **official Microsoft Windows ISO** into a minimal Windows VM for
QEMU/KVM and libvirt. The build is unattended and reproducible, needs no root,
and is not activated (you activate it yourself).

* No Windows Update, Defender, SmartScreen, Copilot/Recall/AI, Edge, OneDrive,
  Store or inbox apps, telemetry, search indexer, hibernation or pagefile. WinRE
  is removed too.
* The OS is applied with **CompactOS**, the component store is reset, and payloads
  of disabled optional features are stripped. The golden image is a zstd-compressed
  qcow2, and you run on a thin overlay, so a reset is instant.
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
sudo dnf install qemu-kvm edk2-ovmf xorriso p7zip p7zip-plugins wimlib-utils samba socat aria2

# 2. Windows ISO: download in a browser from
#    https://www.microsoft.com/software-download/windows11  ("Download Windows 11 Disk Image (ISO)")
#    and save it as ~/Others/vm/iso/Win11_*.iso  (any official Win10/11 ISO works)

# 3. build everything (about 30-60 min, unattended)
./winvm fetch          # pinned virtio-win + OpenSSH, sha256-verified
./winvm editions       # optional: list editions, pick WIN_EDITION
./winvm install        # builds the boot ISO, installs, writes the golden base image

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
| `iso` | **iso→iso**: standalone single-edition installer `winslim-standalone.iso` |
| `install [--gui] [--standalone]` | unattended install, then golden `winslim-base.qcow2` and an overlay |
| `start [--gui]` | start headless (daemonized), or with a GTK window plus clipboard |
| `ssh`, `run`, `cp`, `wait` | CLI access over SSH (key auth, PowerShell) |
| `ga <cmd>` / `ga exec <prog> [args]` | qemu-guest-agent via socket, e.g. `./winvm ga exec ipconfig` |
| `rdp` | FreeRDP session (dynamic resolution, clipboard, host drive) |
| `gui` | GTK window, or a VNC viewer if already running headless |
| `stop` / `kill` / `status` | ACPI shutdown, hard off, overview |
| `reset` | discard all changes (new empty overlay on the base image) |
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
   + MSR + C:, no recovery partition) and runs `dism /Apply-Image /Compact`. From
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
   disables scheduled tasks, runs CompactOS and ResetBase, and re-trims.
4. **First logon** (`firstlogon.ps1`): a single autologon creates the user profile
   inside the base image, clears the autologon secrets and powers off.
5. Host: `qemu-img convert -c` (zstd) gives the read-only **base**, plus a thin
   **overlay** that you actually run.

Progress streams over the serial port (`~/Others/vm/run/winslim/install-serial.log`).
Logs inside the guest are in `C:\Windows\Setup\winslim*.log`.

## Customizing

Put overrides in `config.local.env` (git-ignored). Example:

```sh
WIN_EDITION="Windows 11 Pro N"    # N editions are smaller (no media stack)
WIN_PASSWORD='s3cret'
WIN_KEYBOARD=041f:0000041f        # Turkish Q
RAM=8G CPUS=8 SSH_PORT=2022
SHARE_DIR=$HOME/Projects
```

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
overlay, and QEMU's image locking will refuse.

## Activation and re-enabling things

The VM is unactivated. Run `slmgr /ipk <your-key>` then `slmgr /ato`, or use
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
