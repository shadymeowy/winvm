# winvm: slim, reproducible Windows VMs from YAML image files

`winvm` turns an **official Microsoft Windows ISO** into a minimal Windows VM on
QEMU/KVM: unattended, reproducible, runs as your user, not activated. Each image is
described by one small YAML file, like a Dockerfile: built from an ISO, or on top of
another image. Files refer to each other by name, so bases and images are easy to
share and reuse.

* No Windows Update, Defender, SmartScreen, Copilot/Recall/AI, Edge, OneDrive,
  Store or inbox apps, telemetry, search indexer, hibernation, pagefile or WinRE.
  The component store is reset and payloads of disabled features are stripped.
* Guest tools: virtio drivers, **qemu-guest-agent**, spice-vdagent (clipboard).
* **CLI first.** Headless VM with OpenSSH (keys) and PowerShell. `winvm sh <command>`
  runs a guest command like a local one: exit code, stdin, pipes, Unix line endings,
  current folder, in well under a second.
* **Mounts.** Host folders appear in the guest as `C:\mnt\<name>`, from the config or
  added and removed while the VM runs (`winvm mount ~/proj`).
* **Layers.** Images are zstd-compressed read-only qcow2 layers; instances run on thin
  disks on top. Several instances run at once, each with its own ports.

## Requirements

* Linux x86-64 with KVM (`/dev/kvm` usable by you; on most distros: be in group `kvm`)
* QEMU - the one thing to install:

  ```sh
  sudo dnf install qemu-kvm                      # Fedora
  sudo apt install qemu-system-x86 qemu-utils    # Debian / Ubuntu
  sudo pacman -S qemu-base                       # Arch
  ```

  plus `samba` if you want mounts (host folders in the guest).
* Python ≥ 3.10, 8 GB RAM or more (a VM takes 4 GB by default), and disk space: about
  9 GB for the downloads (mostly the Windows ISO) plus about 6 GB per built `base`,
  25 GB free to be comfortable

Everything else winvm needs comes with it or is downloaded once, each file pinned by
its sha256: the Windows ISO (from Microsoft, into `~/.local/share/winvm/iso`) and, into
`~/.cache/winvm`, virtio-win drivers, Win32-OpenSSH and, if the host has none, UEFI
firmware (Debian's OVMF package). winvm asks before downloading (`-y`: don't ask).
`winvm check` shows what is there and what is missing. winvm runs as your user, never
as root.

## Install

From a checkout of this repository:

```sh
uv tool install .            # or: pipx install .   or: pip install --user .
winvm check                  # what the host has, what will be downloaded
```

Shell completion (generated from winvm's own commands; rerun after updates):

```sh
winvm completion bash > ~/.local/share/bash-completion/completions/winvm
winvm completion fish > ~/.config/fish/completions/winvm.fish
```

## Quick start

```sh
winvm up                     # first time: asks to download, then builds image `base`
winvm sh                     # PowerShell in the guest (exit with `exit`)
winvm sh Get-ComputerInfo -Property OsName,OsBuildNumber
winvm stop
```

No config file is needed: `winvm` uses its built-in one, builds image `base` from the
Windows ISO and starts instance `main` on it. The first `up` lists what it needs to
download, about 8.6 GB in all: the Windows 11 25H2 ISO (English, about 7.7 GB,
straight from Microsoft's servers) and the virtio drivers (about 0.9 GB), and asks
once. Have an ISO already, or want another language? Download it from
<https://www.microsoft.com/software-download/windows11> and use
`winvm up --iso ~/Downloads/Win11_xxx.iso` instead; it is remembered.

**The first build takes a while** - installing and debloating Windows can take up to
an hour, depending on the disk (an SSD helps a lot) and the CPU, plus the download
time. It runs unattended; `winvm` shows the progress, and you can watch the VM with
a VNC viewer at the address it prints. Later builds of the same image are skipped
(it's cached), and `slim: true` (see below) makes rebuilds much faster.
From then on:

```sh
winvm up                     # seconds until SSH answers
winvm sh <command>           # as often as you like, well under a second each
winvm stop
```

`main` keeps everything you do in it across stop/start. `winvm reset` gives it a fresh
Windows again; `winvm save NAME` freezes its current state as an image to come back
to or start other instances from. To bake your own tools into images, write image
files (next section).

## Images

An image is one YAML file; its file name is its name. Either it is built **from an
ISO** (unattended install + debloat, offline), or **from another image** (boot it, run
steps with network, keep the changes):

```yaml
# ~/.config/winvm/images/base.yaml - replaces the built-in base (`winvm config dump base`);
# an ISO image's file only needs what differs from the built-in base
edition: Windows 11 Pro N            # `winvm editions` lists them
keyboard: "041f:0000041f"            # Turkish Q
slim: true                           # cache the debloated image: much faster rebuilds
setup:                               # during the install, as SYSTEM, without network
  - download: {url: "https://www.7-zip.org/a/7z2409-x64.msi", sha256: <sha256>}
  - run: Start-Process msiexec -Wait -ArgumentList '/i', '7z2409-x64.msi', '/qn'
```

```yaml
# ~/.config/winvm/images/dev.yaml
from: base                           # an image name, or a path: ../shared/base.yaml
steps:                               # with network; files land in a work folder
  - download: {url: "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe",
               sha256: <sha256>}
  - run: .\python-3.13.1-amd64.exe /quiet InstallAllUsers=1 PrependPath=1 | Out-Null
  - copy: [./dotfiles, 'C:\Users\user\dotfiles']   # ./ = next to this file
  - restart: true
run:                                 # how instances of dev run (images on dev inherit it)
  ram: 8G
  mounts:
    projects: ~/Projects             # guest C:\mnt\projects
    docs: {path: ~/Documents, readonly: true}
```

```sh
winvm up dev                         # builds base and dev as needed, runs instance "dev"
winvm -i work up --image dev --ram 6G    # another instance of dev, with its own RAM
winvm up ./team/office.yaml          # an image file anywhere: instance "office"
```

**Where image files come from.** A name like `dev` means `dev.yaml` in
`~/.config/winvm/images/` (setting `images_dir`), else a built-in (only `base`). Any
other file is given by its path once; winvm remembers it, so later commands (and
`from: NAME` in other files) work by name from any folder. Like a Docker image, a
built image stays usable by name even when its file is gone (or for images you
`save`): it just can't be rebuilt without the file. Your `base.yaml` replaces the built-in `base` (`winvm images` says so); two
files with the same name are an error, never a silent pick. A file's folder is its
context: `copy: [./dotfiles]` and mount paths are relative to the file. For files
you share, leave `iso:` out: everyone links their own ISO once with `--iso`.

**Steps.** `run` is PowerShell; a thrown error or a non-zero exit fails the build.
`download` fetches on the host, checks the sha256 (get it with
`curl -sL URL | sha256sum`) and puts the file into the work folder. `copy: [HOST]`
puts a file or folder there too, `copy: [HOST, GUEST]` anywhere in the guest
(`from` images only). `restart` reboots (`from` images only). Run steps start in
the work folder, which is deleted when the build ends. Files that `setup` steps use
travel on a CD: their names must be valid on Windows and at most 64 characters long.

**Instances.** `winvm up NAME` runs the instance NAME, on the image of the same name
if there is one, else on `base` (or `--image`). Its settings come from the machine's
`run` defaults, then the `run:` of its image and the images below, then its own
`--ram/--cpus` (kept in its folder).

**Rebuilds work like Docker layers.** Every image has a content hash over its
definition, the files it copies and the image below it (not over where its file is).
Change any of it and the image (and everything built on it) gets a new version on the
next `build`, `reset`, or `up` of a new instance. Existing instances keep running on
the version they were created on; `winvm ps` marks them outdated and `winvm reset`
moves them over (discarding their changes). `winvm prune` deletes versions nothing uses.

**Machine settings** (folders, ports, UUID, RAM reserve, default `run`) are in
`~/.config/winvm/winvm.yaml`, merged over the built-in ones (`winvm config dump`).
They never change what an image contains, so an image file means the same on every
machine.

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

Mounts from `run:` settings are set up at every start; `mount`/`umount` change a
running instance until it stops. Each instance runs its own unprivileged Samba server
(QEMU forwards `\\10.0.2.4` to it), whose config winvm rewrites on the fly. The guest
gets folder links in `C:\mnt` rather than drive letters, because a drive letter
exists only in the logon session that mapped it.

## Commands

Instance commands act on `-i NAME`, default `main`. Wherever an IMAGE is asked for,
a name or the path of an image file works. `-y` (before the command) allows downloads
without asking.

| command | |
|---|---|
| `up [NAME\|FILE...] [--image IMAGE] [--ram R] [--cpus N] [--iso ISO] [--all] [--gui]` | create (building images as needed) and start; ready when SSH answers |
| `stop [NAME...] [--all]` / `kill` | ACPI shutdown (hard kill after 120 s) / power off |
| `ps` | instances: state, ports, disk, image version, mounts |
| `sh [-t] [CMD]`, `ssh`, `cp`, `wait` | see above |
| `mount [PATH [NAME]] [--ro]` / `umount NAME` | host folders in the running guest |
| `gui` / `rdp` | VNC viewer (or start with a GTK window) / FreeRDP session |
| `ga`, `balloon [SIZE]`, `screenshot`, `cmdline` | guest agent, live RAM, PNG of the screen, QEMU argv |
| `build [IMAGE...] [--force]` | build missing or changed images (default: all known) |
| `images` | images: built versions, status, size, base, users, defining file |
| `save NAME` | freeze the instance's disk as image NAME; the instance continues on it |
| `reset [IMAGE]` | discard the instance's changes: fresh disk on the current version |
| `rm` / `rmi IMAGE` / `prune [--cache]` | delete an instance / an image / unused versions and leftovers |
| `check` | what the host has and lacks, what will be downloaded |
| `fetch [IMAGE...]` | download now what building needs (asks; `-y`) |
| `editions [IMAGE]` | editions in an image's Windows ISO |
| `completion bash\|fish` | print a shell completion script |
| `config dump [base]`, `config show [IMAGE]`, `config path` | built-in settings or base / what is in effect / files and folders |

`save` is for states you produce by hand (e.g. after activating something); an
instance's disk keeps its changes across stop/start anyway.

## Where things live

```
~/.config/winvm/winvm.yaml          machine settings (optional)
~/.config/winvm/images/NAME.yaml    your image files (optional)
~/.local/share/winvm/               --data-dir / $WINVM_DATA_DIR / settings.data_dir
  images/base@fb97f6be18d2.qcow2    read-only layers, <name>@<hash>, + .json metadata
  instances/main/disk.qcow2         an instance's own changes, backed by a layer
  iso/  keys/  share/               linked or downloaded ISOs, the SSH key the guests trust, default mount
~/.cache/winvm/                     --cache-dir / $WINVM_CACHE_DIR / settings.cache_dir
  downloads/  slim/  firmware/      pinned downloads, cached slim images, OVMF if downloaded
  build/                            build scratch
$XDG_RUNTIME_DIR/winvm-*/<name>/    while running: pid, ports, sockets, serial.log, ssh_config, smb/
```

Backing files are relative paths, so `qemu-img info --backing-chain` shows the
same chain as `winvm images` and the data folder can be moved.

**Access.** SSH `127.0.0.1:2222` (+*n*), user `user`, key `~/.local/share/winvm/keys/id_ed25519`
(`~/.ssh/id_ed25519.pub` is authorized too). For plain `ssh main` or VS Code
Remote-SSH, add `Include /run/user/*/winvm-*/*/ssh_config` to `~/.ssh/config`.
RDP `127.0.0.1:3390`, user `user`, password `winslim`. VNC `127.0.0.1:5930`.
All forwards bind to 127.0.0.1; the guest firewall is off on purpose.

**Host RAM guard.** Before starting a VM, winvm checks `MemAvailable` minus what
running VMs may still grow into, and refuses when the VM plus `settings.mem_reserve`
doesn't fit.

## How an ISO image is built

```
VM without network:  CD 1  Microsoft's Windows ISO, unmodified (boots; Enter for "press any key")
                     CD 2  winvm payload: autounattend.xml + winslim\ (pe.cmd, lists, unattend.xml,
                           first-boot scripts, your setup steps) - written in pure Python
                     CD 3  virtio-win (drivers, guest tools)
```

1. **WinPE.** Windows Setup finds `autounattend.xml` on CD 2 and runs
   `winslim\start.cmd` → `pe.cmd` (`data/guest/`), which installs Windows itself:
   it refuses to run unless the SMBIOS vendor is QEMU, partitions disk 0 (ESP + MSR +
   C:, no recovery), applies the edition and services it offline with the image's own
   DISM: AppX packages not in `appx-keep.txt`, capabilities, features, payloads,
   ResetBase, registry edits, services, Edge/OneDrive/WinRE files (all in
   `data/guest/lists/`). Then drivers, unattend, first-boot scripts, bcdboot, reboot.
2. **Setup/OOBE**, unattended and offline.
3. **SetupComplete** (SYSTEM): guest tools, OpenSSH, .NET native images (PowerShell
   starts in 0.2 s instead of 2 s), firewall/power settings, an online debloat
   sweep, your `setup` steps, cleanup and trim.
4. **First logon** creates the profile and removes WebView2; one more boot deletes
   what was locked, trims and powers off.
5. Host: `qemu-img convert` (zstd) writes the layer.

With `slim: true` WinPE instead captures the serviced image right after step 1 onto
an extra disk, which winvm keeps in `~/.cache/winvm/slim`; from then on builds
attach it and apply it directly, skipping the servicing. Its cache key is the ISO
(by sha256), the edition, the debloat lists, pe.cmd and the virtio drivers, so it is
shared by every image using them.

Progress (from step 3 on) streams from the build VM's serial port; inside the guest
the logs are in `C:\Windows\Setup\winslim*.log`. `WINVM_DEBUG=1 winvm build` makes a
WinPE failure stay on screen (`winvm -i _install-NAME screenshot`) instead of
powering off.

## What to expect

Measured with Windows 11 Pro 25H2, 4 vCPUs / 4 GB, KVM, on an SSD; times vary with the
host, so only the relations are worth remembering:

* **First build** of `base`: up to about an hour, plus downloading about 8.6 GB.
  A `slim` image's first build captures the debloated image too and takes a bit
  longer; after that, rebuilds of it take roughly half the time.
* **An image `from` another**: about a minute plus whatever its steps do.
* **Daily use**: an instance boots to SSH in seconds; `winvm sh` commands return in
  well under a second.
* **Disk**: the base layer is about 5.5 GB (Microsoft's ISO: 7-9 GB); a fresh
  instance disk is under 1 MB and grows with what you change; a cached slim image is
  about 5 GB.
* **RAM**: an idle headless guest uses about 1 GB inside, but Windows touches all of
  its RAM at boot, so the host gives it the full `ram`; `winvm balloon 2G` hands the
  rest back while it runs.

CompactOS (`debloat.compact_os`) reaches only ~1.8:1 and its output doesn't
compress further, so raw guest data plus qcow2 zstd is smaller and the default.

## Activation, Office, re-enabling things

The VM is unactivated; Windows is Microsoft software under Microsoft's license
terms, and activating it needs a license of your own. Every VM has the same SMBIOS
UUID (`settings.uuid`), so all images and instances look like one machine to
activation: activate in an instance and `winvm save` it, and rebuilding or resetting
keeps it. That is meant for one licensed machine: running several activated copies
at the same time needs a license for each (give them different `uuid`s via
separate data dirs). `slmgr /ipk <key>` then `slmgr /ato`, or Settings › Activation.

Office installs fine as an image step or by hand with the [Office Deployment Tool][odt],
which downloads from Microsoft's CDN, not Windows Update. Microsoft 365 Apps need
the WebView2 runtime (`debloat.remove_webview: false`, or `winget install
Microsoft.EdgeWebView2Runtime`) and more than 4 GB RAM without a pagefile.

Updates are blocked by policy (a WSUS at 127.0.0.1) and disabled services. To undo:
delete `HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate` and set `wuauserv`,
`UsoSvc` and `WaaSMedicSvc` back to manual (Start=3).

[odt]: https://learn.microsoft.com/microsoft-365-apps/deploy/overview-office-deployment-tool

## Notes

* **The Windows download.** winvm downloads one pinned ISO (`settings.windows`:
  Windows 11 25H2, English) from Microsoft's own CDN and checks its sha256. It does
  not script Microsoft's download page or use third-party mirrors; for other
  languages or versions, download the ISO yourself and pass `--iso`, or pin another
  one in `settings.windows` (url + sha256 + size). An ISO you bring is identified by
  its sha256 too; winvm says whether it matches the one it knows.
* **pe.cmd wipes disk 0** of the VM it runs in. It refuses to run unless the SMBIOS
  vendor is QEMU, and a slim capture only writes to a disk of exactly the capture size.
* The debloat lists (what gets removed) ship inside the package. To change them,
  edit `src/winvm/data/guest/lists/*.txt` in a checkout and install it with
  `uv tool install -e .` (editable); images rebuild because their hash changes.
