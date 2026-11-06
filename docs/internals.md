# How it works

[Back to the README](../README.md)

- [Layers](#layers)
- [How an ISO image is built](#how-an-iso-image-is-built)
- [What to expect](#what-to-expect)
- [Debugging a build](#debugging-a-build)
- [What winvm downloads](#what-winvm-downloads)

## Layers

Images are zstd-compressed, read-only qcow2 layers. An image built `from` another stores only
what its steps changed, and instances run on thin writable disks on top:

```
images/base@4c03....qcow2        read-only, compressed
  └ images/dev@ab12....qcow2     read-only, compressed: only what its steps changed
      └ instances/work/disk      writable, not compressed: what you do in the VM
```

`winvm rm` deletes only the top disk. `winvm rmi` refuses while another image or an instance
sits on the image. `winvm save` turns the top disk into a new compressed layer.

## How an ISO image is built

```
VM without network:  CD 1  Microsoft's Windows ISO, unmodified (boots; Enter for "press any key")
                     CD 2  winvm payload: autounattend.xml + winslim\ (pe.cmd, lists, unattend.xml,
                           first-boot scripts, your setup steps), written in pure Python
                     CD 3  virtio-win (drivers, guest tools)
```

1. **WinPE.** Windows Setup finds `autounattend.xml` on CD 2 and runs `winslim\start.cmd`, then
   `pe.cmd` (`data/guest/`), which installs Windows itself. It refuses to run unless the SMBIOS
   vendor is QEMU, partitions disk 0 (ESP + MSR + C:, no recovery), applies the edition and
   services it offline with the image's own DISM: AppX packages not in `appx-keep.txt`,
   capabilities, features, payloads, ResetBase, registry edits, services, Edge/OneDrive/WinRE
   files (all in `data/guest/lists/`). Then drivers, unattend, first-boot scripts, bcdboot,
   reboot.
2. **Setup/OOBE**, unattended and offline.
3. **SetupComplete** (SYSTEM): guest tools, OpenSSH, .NET native images (PowerShell starts in
   0.2 s instead of 2 s), firewall and power settings, an online debloat sweep, your `setup`
   steps, cleanup and trim.
4. **First logon** creates the profile and removes WebView2. One more boot deletes what was
   locked, trims and powers off.
5. **Host:** `qemu-img convert` (zstd) writes the layer.

**`pe.cmd` wipes disk 0** of the VM it runs in. That's why it refuses to run unless the SMBIOS
vendor is QEMU, and why a slim capture only writes to a disk of exactly the capture size.

**With `slim: true`,** WinPE instead captures the serviced image right after step 1 onto an
extra disk, which winvm keeps in `~/.cache/winvm/slim`. From then on builds attach it and
apply it directly, skipping the servicing. Its cache key is the ISO (by sha256), the edition,
the debloat lists, `pe.cmd` and the virtio drivers, so every image using them shares it.

## What to expect

Measured with Windows 11 Pro 25H2, 4 vCPUs and 4 GB, on KVM and an SSD. Times vary with the
host, so only the relations are worth remembering.

| | |
|---|---|
| **First build of `base`** | Up to about an hour, plus downloading about 8 GB |
| **First `slim` build** | A bit longer, because it also captures the debloated image; later rebuilds skip the debloat and take a fraction of the time |
| **An image `from` another** | About a minute, plus whatever its steps do |
| **Starting an instance** | Seconds until SSH answers |
| **A `winvm sh` command** | Well under a second |
| **Base layer** | About 5.5 GB (Microsoft's ISO: 7-9 GB) |
| **Fresh instance disk** | Under 1 MB, growing with what you change |
| **Cached slim image** | About 6 GB |
| **Idle headless guest** | About 1 GB in use inside. Windows touches all of its RAM at boot, so the host gives it the full `ram`; `winvm balloon 2G` hands the rest back while it runs. |

CompactOS (`debloat.compact_os`) reaches only about 1.8:1 and its output doesn't compress
further, so raw guest data plus qcow2 zstd is smaller, and that's the default.

## Debugging a build

- Progress lines stream from the build VM's serial port from step 3 on. Before that, step 1 can
  take tens of minutes, and winvm prints a line a minute with the time and the data written.
- Follow them with `winvm -i _install-NAME logs serial -f`, and look at the screen with
  `winvm -i _install-NAME screenshot`.
- `WINVM_DEBUG=1 winvm build` keeps a WinPE failure on screen instead of powering off.
- Inside the guest, the logs are in `C:\Windows\Setup\winslim*.log`.

## What winvm downloads

Everything except QEMU comes with winvm or is downloaded once, each file pinned by its sha256.
winvm asks first (`-y`: don't ask).

| File | From | Into |
|---|---|---|
| Windows ISO | Microsoft | `~/.local/share/winvm/iso` |
| virtio-win drivers | Fedora | `~/.cache/winvm/downloads` |
| Win32-OpenSSH | Microsoft (GitHub) | `~/.cache/winvm/downloads` |
| UEFI firmware, only if the host has none | Debian's OVMF package | `~/.cache/winvm/firmware` |
