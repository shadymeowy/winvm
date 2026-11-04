# winvm: slim, reproducible Windows VMs from one YAML file

`winvm` turns an **official Microsoft Windows ISO** into a minimal Windows VM on
QEMU/KVM: unattended, reproducible, runs as your user, not activated. Images
build on each other like Dockerfiles, and instances run like compose services, all
described in one `winvm.yaml`.

* No Windows Update, Defender, SmartScreen, Copilot/Recall/AI, Edge, OneDrive,
  Store or inbox apps, telemetry, search indexer, hibernation, pagefile or WinRE.
  The component store is reset and payloads of disabled features are stripped.
* Guest tools: virtio drivers, **qemu-guest-agent**, spice-vdagent (clipboard).
* **CLI first.** Headless VM with OpenSSH (keys) and PowerShell. `winvm sh <command>`
  runs a guest command like a local one: exit code, stdin, pipes, Unix line endings,
  current folder. About 0.6 s per command.
* **Mounts.** Host folders appear in the guest as `C:\mnt\<name>`, from the config or
  added and removed while the VM runs (`winvm mount ~/proj`).
* **Layers.** Images are zstd-compressed read-only qcow2 layers; instances run on thin
  disks on top. Several instances run at once, each with its own ports.

## Requirements

* Linux x86-64 with KVM (`/dev/kvm` usable by you; on most distros: be in group `kvm`)
* Python ≥ 3.10, about 25 GB free disk, 8 GB RAM or more (a VM takes 4 GB by default)
* an official Windows 10/11 ISO, downloaded in a browser from
  <https://www.microsoft.com/software-download/windows11>
* host tools:

  ```sh
  # Fedora
  sudo dnf install qemu-kvm edk2-ovmf xorriso p7zip p7zip-plugins wimlib-utils samba guestfs-tools
  # Debian / Ubuntu (package names not tested)
  sudo apt install qemu-system-x86 ovmf xorriso p7zip-full wimtools samba libguestfs-tools
  ```

  `samba` is only needed for mounts, `guestfs-tools` for `slim` images and a small
  cleanup step. `winvm` itself runs as your user, never as root.

## Install

From a checkout of this repository:

```sh
uv tool install .            # or: pipx install .   or: pip install --user .
winvm --help                 # every command also has --help
```

## Quick start

```sh
winvm up --iso ~/Downloads/Win11_25H2_English_x64.iso   # first time ~22 min: builds image `base`
winvm sh                     # PowerShell in the guest (exit with `exit`)
winvm sh Get-ComputerInfo -Property OsName,OsBuildNumber
winvm stop
```

No config file is needed: `winvm` uses its built-in one, builds image `base` from the
ISO and starts instance `main` on it. The `--iso` is remembered (linked into
`~/.local/share/winvm/iso`), so from now on it's just:

```sh
winvm up                     # ~15 s until SSH answers
winvm sh <command>           # as often as you like, ~0.6 s each
winvm stop
```

`main` keeps everything you do in it across stop/start. `winvm reset` gives it a fresh
Windows again; `winvm save NAME` freezes its current state as an image to come back
to or start other instances from. To bake your own tools into images, write a
`winvm.yaml` (next section).

## winvm.yaml

`winvm config dump > winvm.yaml` writes the built-in config with all its comments.
Your file is merged over it, so it only needs what it changes or adds (`null` drops a
built-in image, instance or mount, e.g. `instances: {main: null}`). winvm uses
the first of `-f FILE`, `$WINVM_CONFIG`, `./winvm.yaml`, `~/.config/winvm/winvm.yaml`.

```yaml
settings:
  ports: {ssh: 2222, rdp: 3390, vnc: 5930}    # instance n gets port + n

images:
  base:                                  # from an ISO: unattended install + debloat, offline
    iso: ~/Downloads/Win11_25H2_English_x64.iso
    edition: Windows 11 Pro N            # `winvm editions` lists them
    keyboard: "041f:0000041f"            # Turkish Q
    slim: true                           # cache the debloated image: rebuilds ~10 min
    setup:                               # runs during the install, as SYSTEM, without network
      - download: {url: "https://www.7-zip.org/a/7z2409-x64.msi", sha256: <sha256>}
      - run: Start-Process msiexec -Wait -ArgumentList '/i', '7z2409-x64.msi', '/qn'

  dev:                                   # from another image: boot it, run steps, keep the changes
    from: base
    steps:                               # with network; files land in a work folder
      - download: {url: "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe",
                   sha256: <sha256>}
      - run: .\python-3.13.1-amd64.exe /quiet InstallAllUsers=1 PrependPath=1 | Out-Null
      - copy: [./dotfiles, 'C:\Users\user\dotfiles']
      - restart: true

instances:
  main: {}                               # on base, 4G, 4 CPUs, mount `share`
  work:
    image: dev
    ram: 8G
    mounts:
      projects: ~/Projects               # guest C:\mnt\projects
      docs: {path: ~/Documents, readonly: true}
```

**Steps.** `run` is PowerShell; a thrown error or a non-zero exit fails the build.
`download` fetches on the host, checks the sha256 (get it with
`curl -sL URL | sha256sum`) and puts the file into the work folder. `copy: [HOST]`
puts a file or folder there too, `copy: [HOST, GUEST]` anywhere in the guest
(`from` images only). `restart` reboots (`from` images only). Run steps start in
the work folder, which is deleted when the build ends.

**Rebuilds work like Docker layers.** Every image has a content hash over its
definition, the files it copies and the image below it. Change any of it and the
image (and everything built on it) gets a new version on the next `build`, `reset`,
or `up` of a new instance. Existing instances keep running on the version they were
created on; `winvm ps` marks them outdated and `winvm reset` moves them over
(discarding their changes). `winvm prune` deletes versions nothing uses.

## Running commands in the guest: `winvm sh`

```sh
winvm sh                          # interactive shell
winvm sh ipconfig /all            # a command line; PowerShell parses it
winvm sh 'gci C:\ | select Name'  # one quoted argument = PowerShell code as is
winvm sh notepad "a b.txt"        # words with spaces stay one argument
winvm sh -t python                # -t: a terminal, for interactive programs
cat x | winvm sh findstr foo      # stdin reaches native programs ...
cat x | winvm sh 'stdin | sort'   # ... and PowerShell code reads it with `stdin`
winvm sh ipconfig | grep IPv4     # output has Unix line endings
winvm sh cmd /c exit 3; echo $?   # 3: the exit code is the command's (1 for a failed cmdlet)
cd ~/Projects/x && winvm -i work sh dir   # inside a mount: runs in C:\mnt\projects\x
```

The command travels base64-encoded, so no quoting is lost between your shell, ssh,
sshd and Windows; all commands to an instance share one SSH connection. `winvm ssh`
is plain ssh (`winvm ssh -L 8080:localhost:80`, `winvm ssh hostname`),
`winvm cp a.txt :C:/Users/user/` is scp, and
`winvm ga exec PROG [ARGS]` goes through the guest agent without network.

## Mounts

```sh
winvm mount ~/data                # running instance: guest C:\mnt\data, at once
winvm mount ~/data d2 --ro        # another name, read-only
winvm mount                       # list
winvm umount d2
```

Mounts in the config are set up at every start; `mount`/`umount` change a running
instance until it stops. Each instance runs its own unprivileged Samba server
(QEMU forwards `\\10.0.2.4` to it), whose config winvm rewrites on the fly. The guest
gets folder links in `C:\mnt` rather than drive letters, because a drive letter
exists only in the logon session that mapped it.

## Commands

Instance commands act on `-i NAME`, default the first instance in the config (`main`).

| command | |
|---|---|
| `up [NAME...] [--all] [--iso ISO] [--image IMG] [--gui]` | create (building images as needed) and start; ready when SSH answers |
| `stop [NAME...] [--all]` / `kill` | ACPI shutdown (hard kill after 120 s) / power off |
| `ps` | instances: state, ports, disk, image version, mounts |
| `sh [-t] [CMD]`, `ssh`, `cp`, `wait` | see above |
| `mount [PATH [NAME]] [--ro]` / `umount NAME` | host folders in the running guest |
| `gui` / `rdp` | VNC viewer (or start with a GTK window) / FreeRDP session |
| `ga`, `balloon [SIZE]`, `screenshot`, `cmdline` | guest agent, live RAM, PNG of the screen, QEMU argv |
| `build [IMAGE...] [--force]` | build missing or changed images (default: all in the config) |
| `images` | config images and built versions: status, size, base, users |
| `save NAME` | freeze the instance's disk as image NAME; the instance continues on it |
| `reset [IMAGE]` | discard the instance's changes: fresh disk on the current version |
| `rm` / `rmi IMAGE` / `prune [--cache]` | delete an instance / an image / unused versions and leftovers |
| `iso [IMAGE] [-o FILE]` | standalone installer ISO of an image (slim image + payload) |
| `editions [IMAGE]`, `fetch` | editions in the ISO / download everything pinned |
| `config dump\|show\|path` | built-in config / merged config / files and folders in use |

`save` is for states you produce by hand (e.g. after activating something); an
instance's disk keeps its changes across stop/start anyway.

## Where things live

```
~/.config/winvm/winvm.yaml          your config (optional)
~/.local/share/winvm/               --data-dir / $WINVM_DATA_DIR / settings.data_dir
  images/base@fb97f6be18d2.qcow2    read-only layers, <name>@<hash>, + .json metadata
  instances/main/disk.qcow2         an instance's own changes, backed by a layer
  iso/  keys/  share/               linked ISOs, the SSH key the guests trust, default mount
~/.cache/winvm/                     --cache-dir / $WINVM_CACHE_DIR / settings.cache_dir
  downloads/  slim/  build/         pinned downloads, cached slim images, build scratch
$XDG_RUNTIME_DIR/winvm-*/<name>/    while running: pid, ports, sockets, serial.log, ssh_config, smb/
```

Backing files are relative paths, so `qemu-img info --backing-chain` shows the
same chain as `winvm images` and the data folder can be moved.

**Access.** SSH `127.0.0.1:2222` (+*n*), user `user`, key `~/.local/share/winvm/keys/id_ed25519`
(`~/.ssh/id_ed25519.pub` is authorized too). For plain `ssh main` or VS Code
Remote-SSH, add `Include /run/user/*/winvm-*/*/ssh_config` to `~/.ssh/config`.
RDP `127.0.0.1:3390`, user `user`, password `winslim`. VNC `127.0.0.1:5930`.
All forwards bind to 127.0.0.1; the guest firewall is off on purpose.

**Host RAM guard.** Before starting a VM or a slim-image export, winvm checks
`MemAvailable` minus what running VMs may still grow into, and refuses when the job
plus `settings.mem_reserve` doesn't fit.

## How an ISO image is built

```
Win11.iso ─┬─ boot.wim (WinPE) ─ wimlib: shell → winslim (pe.cmd)
           │     + drivers (virtio) + unattend.xml + first-boot payload + setup steps
           └─ install.wim ◄── applied by pe.cmd from the 2nd CD
```

1. **WinPE** (`data/guest/pe.cmd`, instead of setup.exe) refuses to run unless the
   SMBIOS vendor is QEMU, partitions disk 0 (ESP + MSR + C:, no recovery), applies
   the image and services it offline with the image's own DISM: AppX packages not in
   `appx-keep.txt`, capabilities, features, payloads, ResetBase, registry edits,
   services, Edge/OneDrive/WinRE files (all in `data/guest/lists/`). Then drivers,
   unattend, payload, bcdboot.
2. **Setup/OOBE**, unattended and offline (the build VM has no network card).
3. **SetupComplete** (SYSTEM): guest tools, OpenSSH, .NET native images (PowerShell
   starts in 0.2 s instead of 2 s), firewall/power settings, an online debloat
   sweep, your `setup` steps, cleanup and trim.
4. **First logon** creates the profile, removes WebView2 and powers off.
5. Host: libguestfs removes WebView2 leftovers; `qemu-img convert` (zstd) writes the layer.

With `slim: true` WinPE instead captures the serviced image right after step 1;
the host recompresses it (solid LZMS, 2 threads, low RAM) into `~/.cache/winvm/slim`,
and from then on builds apply it directly and skip the servicing. The cache key is
the ISO, edition and debloat lists, so it is shared by every image using them.
`winvm iso` wraps it into a standalone installer ISO.

Progress streams from the build VM's serial port; inside the guest the logs are in
`C:\Windows\Setup\winslim*.log`. `WINVM_DEBUG=1 winvm build` makes a WinPE failure
drop to a shell (watch with VNC or `winvm screenshot`) instead of powering off.

## Results

Windows 11 Pro 25H2 (build 26300), 4 vCPU / 4 GB, KVM:

| | |
|---|---|
| `base` from Microsoft's ISO | ~22 min |
| first `slim` build (capture + export + install) | ~70 min |
| later `slim` builds | **~10 min** |
| an image `from` another, a few steps | ~1 min + the steps |
| boot to SSH | ~12 s |
| `winvm sh hostname` | **~0.6–0.7 s** |
| base layer | **5.5 GB** (Microsoft's ISO is 9.0 GB) |
| slim image in the cache | 3.6 GB |
| fresh instance disk | < 1 MB |
| idle guest RAM (headless) | ~950 MB, 86 processes |
| host RAM of a 4 GB instance | 4.1 GB (Windows zeroes all its RAM at boot); `winvm balloon 2G` → 2.1 GB |

CompactOS (`debloat.compact_os`) reaches only ~1.8:1 and its output doesn't
compress further, so raw guest data plus qcow2 zstd is smaller and the default.

## Activation, Office, re-enabling things

The VM is unactivated. Every VM has the same SMBIOS UUID (`settings.uuid`), so all
images and instances look like one machine to activation: activate in an instance
and `winvm save` it. `slmgr /ipk <key>` then `slmgr /ato`, or Settings › Activation.

Office installs fine as an image step or by hand with the [Office Deployment Tool][odt],
which downloads from Microsoft's CDN, not Windows Update. Microsoft 365 Apps need
the WebView2 runtime (`debloat.remove_webview: false`, or `winget install
Microsoft.EdgeWebView2Runtime`) and more than 4 GB RAM without a pagefile.

Updates are blocked by policy (a WSUS at 127.0.0.1) and disabled services. To undo:
delete `HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate` and set `wuauserv`,
`UsoSvc` and `WaaSMedicSvc` back to manual (Start=3).

[odt]: https://learn.microsoft.com/microsoft-365-apps/deploy/overview-office-deployment-tool

## Notes

* The ISO from `winvm iso` wipes disk 0 without asking; it refuses to run unless
  the SMBIOS vendor is QEMU (create `winslim\force` on the media to override).
* Windows ISOs have to be downloaded by hand; winvm doesn't script Microsoft's site.
* The debloat lists (what gets removed) ship inside the package. To change them,
  edit `src/winvm/data/guest/lists/*.txt` in a checkout and install it with
  `uv tool install -e .` (editable); images rebuild because their hash changes.
