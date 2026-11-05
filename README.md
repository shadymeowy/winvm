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
* **Mounts.** Host folders appear in the guest as `C:\mnt\<name>` or any folder path you
  pick, from the config or added and removed while the VM runs (`winvm mount ~/proj`).
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

  plus the OpenSSH client (`ssh`, usually there already), and `samba` if you want
  mounts (host folders in the guest). winvm's errors say what to install on your distro.
* Python ≥ 3.10, 8 GB RAM or more (a VM takes 4 GB by default), and disk space: about
  9 GB for the downloads (mostly the Windows ISO) plus about 6 GB per built `base`,
  25 GB free to be comfortable

Everything else winvm needs comes with it or is downloaded once, each file pinned by
its sha256: the Windows ISO (from Microsoft, into `~/.local/share/winvm/iso`) and, into
`~/.cache/winvm`, virtio-win drivers, Win32-OpenSSH and, if the host has none, UEFI
firmware (Debian's OVMF package). winvm asks before downloading (`-y`: don't ask).
`winvm check` shows what is there and what is missing. winvm runs as your user and
refuses to run as root.

## Install

From PyPI:

```sh
pipx install winvm           # or: uv tool install winvm   or: pip install winvm
winvm check                  # what the host has, what will be downloaded
```

Upgrade with `pipx upgrade winvm` (or `uv tool upgrade winvm`); built images and
instances stay. A version that builds `base` differently marks it *outdated* in
`winvm images`, but it stays in use: Windows is installed again only when you run
`winvm build base` (which asks first). The source is at
<https://github.com/shadymeowy/winvm>; from a checkout: `uv tool install .` (or `pipx
install .`).

Shell completion: commands and options come from winvm's own definitions (generate it
again after an upgrade); instance, image and mount names are looked up live when you
press Tab:

```sh
winvm completion bash > ~/.local/share/bash-completion/completions/winvm
winvm completion fish > ~/.config/fish/completions/winvm.fish
# zsh: autoload -U bashcompinit && bashcompinit && source <(winvm completion bash)
```

**Uninstall:** `winvm stop --all`, then `pipx uninstall winvm` (or `uv tool uninstall
winvm`) and delete what it made: `~/.local/share/winvm` (images, instances, ISO),
`~/.cache/winvm`, `~/.config/winvm` and the completion file. `winvm df` shows what
takes the space.

## Quick start

```sh
winvm up                     # first time: asks to download, then builds image `base`
winvm sh                     # PowerShell in the guest (exit with `exit`)
winvm sh Get-ComputerInfo -Property OsName,OsBuildNumber
winvm rdp                    # a desktop (FreeRDP); or winvm gui (VNC viewer / QEMU window)
winvm stop
```

The guest's user is `user` with password `winslim` (the image's `user:` and
`password:`; SSH uses keys).

No config file is needed: `winvm` uses its built-in one, builds image `base` from the
Windows ISO and starts instance `main` on it. `winvm sh` (or `ssh`, `cp`, `rdp`) also
creates `main` when it doesn't exist yet, so `winvm sh` alone works from scratch; other
instance names have to be created with `up` first. The first `up` lists what it needs to
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
to or start other instances from:

```sh
winvm stop                          # save needs the instance stopped
winvm save golden                   # main's disk becomes image "golden" (compressed, read-only)
winvm up work --image golden        # a second instance, "work", starting from it
winvm sh -i work                    # -i picks the instance (default: main)
winvm reset                         # later: main back to "golden", dropping what came after
```

`main` continues on the saved image, so the image itself never changes. A saved image
can't be rebuilt (there is no recipe) and stays on the `base` it was made from. To
bake your own tools into images that rebuild on a newer `base`, write image files
(next section).

## Images

An image is one YAML file; its file name is its name. Either it is built **from an
ISO** (unattended install + debloat, offline), or **from another image** (boot it, run
steps with network, keep the changes):

```yaml
# ~/.config/winvm/images/base.yaml - replaces the built-in base (`winvm config dump base`);
# an ISO image's file only needs what differs from the built-in base
edition: Windows 11 Pro N            # `winvm editions` lists them
timezone: Pacific Standard Time      # Windows time zone name (`tzutil /l` lists them)
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
    src: {path: ~/src, at: 'C:\src'}  # anywhere in the guest instead of C:\mnt
```

```sh
winvm up dev                         # builds base and dev as needed, runs instance "dev"
winvm up dev2 --image dev --ram 6G   # another instance of dev, with its own RAM
winvm up ./team/accounting.yaml      # an image file anywhere: instance "accounting"
```

**Where image files come from.** A name like `dev` means `dev.yaml` in
`~/.config/winvm/images/` (setting `images_dir`), else a built-in (only `base`). Any
other file is given by its path once; winvm remembers it, so later commands (and
`from: NAME` in other files) work by name from any folder. Like a Docker image, a
built image stays usable by name even when its file is gone (or for images you
`save`): it just can't be rebuilt without the file (`prune` keeps its newest version,
`rmi` deletes it). Your `base.yaml` replaces the built-in `base` (`winvm images` says
so); two files with the same name are an error, never a silent pick. A file with a
mistake fails only the commands that use it (`winvm images` lists it as broken). A file's folder is its
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
if there is one (a saved one too), else on `base` (or `--image`). Its settings come
from the machine's `run` defaults, then the `run:` of its image and the images below,
then its own: `--ram/--cpus` and `mount --save` (kept in its folder; `winvm info`
shows them, `--ram default` drops one).

**Rebuilds work like Docker layers.** Every image has a content hash over its
definition, the files it copies and the image below it (not over where its file is).
Change a `from` image and it (and everything built on it) gets a new version on the
next `build`, `reset`, or `up` of a new instance. An ISO image is different, because
installing Windows again takes up to an hour: when its file (or a winvm upgrade)
changes it, `winvm images` marks it *outdated*, and it stays in use until you run
`winvm build NAME`. Existing instances keep running on the version they were created
on; `winvm ps` marks them outdated and `winvm reset` moves them over (discarding their
changes). `winvm prune` deletes versions nothing uses.

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
winvm sh cmd /c exit 3; echo $?   # 3: the exit code is the command's (fish: $status)
cd ~/Projects/x && winvm sh -i dev dir    # inside a mount: runs in C:\mnt\projects\x
```

The command travels base64-encoded, so no quoting is lost between your shell, ssh,
sshd and Windows; all commands to an instance share one SSH connection. `winvm ssh`
is plain ssh (`winvm ssh -L 8080:localhost:80`, `winvm ssh hostname`),
`winvm cp a.txt :C:/Users/user/` is scp, and
`winvm ga exec PROG [ARGS]` goes through the guest agent without network.

## Dynamic memory

```yaml
run:
  ram: {min: 2G, max: 8G}            # or: winvm up --ram 2G-8G
```

The VM gets `max`, and winvm keeps it near what the guest actually uses: a small
helper per instance reads the guest's memory stats (virtio-balloon) and moves the
balloon - it grows the guest at once when free memory runs short, and gives memory
back to the host after it has been free for half a minute, step by step. The guest
also gets a pagefile (added on its first start in this mode, one reboot), which
absorbs a sudden large allocation while memory flows back in. If the helper stops or
the guest's stats go silent, the guest gets its full `max` back; `winvm up`/`sh`
restart a missing helper. `winvm ps` shows the current size (`2.1/8G`); the log is
`balloon.log` in the instance's run folder.

Windows touches all of its RAM at boot, so without this a 4 GB instance takes 4 GB
of the host as long as it runs. The RAM guard still counts `max` when starting VMs:
dynamic memory leaves room for the host, it doesn't fit more VMs than `max` allows.
Several instances on the same image can also share identical memory pages with the
kernel's KSM (off by default; `winvm check` shows the command to turn it on).

## Mounts

```sh
winvm mount ~/data                # running instance: guest C:\mnt\data, at once
winvm mount ~/data d2 --ro        # another name (C:\mnt\d2), read-only
winvm mount ~/src 'C:\src'        # a Windows path instead of a name: the link goes there
winvm mount ~/data --save         # also at every later start of this instance
winvm mount                       # list
winvm umount d2                   # by name, or where it is: winvm umount 'C:\src'
```

Mounts from `run:` settings are set up at every start; `mount`/`umount` change a
running instance until it stops, or for good with `--save` (which also works on a
stopped instance; `umount --save` drops even a mount the image's `run:` adds). Each instance runs its own unprivileged Samba server
(QEMU forwards `\\10.0.2.4` to it), whose config winvm rewrites on the fly. The guest
gets folder links (in `C:\mnt`, or where `at:` / a Windows path puts them) rather than
drive letters, because a drive letter exists only in the logon session that mapped it.
winvm only ever removes links it made itself (the guest keeps a list of them); a
mount point taken by a real folder or file is refused, never replaced.

## Commands

Instance commands act on `-i NAME` (default `main`), given before or after the command
(`winvm sh -i dev`, `winvm -i dev sh`; for `ssh` and `cp` only before, since ssh and scp
have their own `-i`). Wherever an IMAGE is asked for, a name or the path of an image
file works. Deleting something asks first in a terminal; `-y` doesn't ask (neither
before downloads).

| command | |
|---|---|
| `up [NAME\|FILE...] [--image IMAGE] [--ram R\|MIN-MAX\|default] [--cpus N\|default] [--iso ISO] [--all] [--gui]` (`start`) | create (building images as needed) and start; ready when SSH answers |
| `stop [NAME...] [--all] [--timeout S]` (`down`) / `restart [NAME...]` / `kill [NAME...]` | ACPI shutdown, all at once, hard power-off after 120 s / stop + start / power off |
| `ps [-q] [--json]` (`status`), `info` | instances: state, ports, disk, RAM, image version, mounts / one instance in detail |
| `sh [-t] [CMD]` (`shell`), `ssh`, `cp`, `wait [SECONDS]` | see above; `wait`: until SSH answers (default 600 s) |
| `mount [PATH [NAME\|WINPATH]] [--ro] [--save]` / `umount NAME\|WINPATH [--save]` | host folders in the guest |
| `gui` / `rdp` | VNC viewer (or start with a GTK window) / FreeRDP session |
| `logs [serial\|balloon\|smb\|qemu] [-f]` | an instance's logs (without a name: where they are) |
| `ga`, `balloon [SIZE]`, `screenshot`, `cmdline` | guest agent, live RAM, PNG of the screen, QEMU argv |
| `build [IMAGE...] [--force] [--gui]` | build missing or changed images (default: all known) |
| `images [--json]` | images: built versions, status, size, base, users, defining file |
| `save NAME` | freeze the instance's disk as image NAME; the instance continues on it |
| `reset [IMAGE]` | discard the instance's changes: fresh disk on the current version (of IMAGE) |
| `rm [NAME...]` / `rmi IMAGE` / `prune [-n] [--cache] [--iso]` | delete instances / an image / unused versions, stale slim images, leftovers (`--cache`: downloads and slim images, `--iso`: ISOs) |
| `df` | disk space used by images, instances, ISOs, cache; free space |
| `check` | what the host has and lacks, what will be downloaded |
| `fetch [IMAGE...]` | download now what building needs (asks; `-y`) |
| `editions [IMAGE\|ISO]` | editions in an image's (or an ISO file's) Windows ISO |
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
$XDG_RUNTIME_DIR/winvm-*/<name>/    while running: pid, ports, sockets, logs, ssh_config, smb/
```

`winvm config path` prints the folders in use (the run folder's name is a hash of the
data folder, so two data folders never mix).

**Saving space.** Once an image is built it stands on its own: `winvm prune --cache
--iso` removes the slim images, downloads and ISOs, and images and instances keep
working, frozen as they are. Only building an ISO image again needs an ISO (winvm can
download its pinned one; `prune --iso` says which ISOs those are), and without the slim
cache that build takes longer. Don't delete `images/` or `keys/`: `images/` holds the
images themselves (`winvm rmi` asks first), `keys/` the key winvm reaches the guests with.

Backing files are relative paths, so `qemu-img info --backing-chain` shows the
same chain as `winvm images` and the data folder can be moved.

**Access.** SSH `127.0.0.1:2222` (+*n*), user `user`, key `~/.local/share/winvm/keys/id_ed25519`;
your own keys in `settings.ssh_pubkeys` (default `~/.ssh/id_ed25519.pub`) are
authorized too, brought up to date at every start. For plain `ssh main` or VS Code
Remote-SSH, put `Include /run/user/*/winvm-*/*/ssh_config` at the **top** of
`~/.ssh/config` (before any `Host` line; an instance's entry exists while it runs).
RDP `127.0.0.1:3390`, user `user`, password `winslim` (the image's `password:`). VNC
`127.0.0.1:5930`. All forwards bind to 127.0.0.1; the guest firewall is off on purpose.

**Host RAM guard.** Before starting a VM, winvm checks `MemAvailable` minus what
running VMs may still grow into, and refuses when the VM plus `settings.mem_reserve`
doesn't fit: stop another instance, or give this one less (`winvm up --ram 3G`, or
`--ram 2G-6G` for dynamic memory).

## Troubleshooting

* **Logs**: `winvm logs` lists an instance's logs (`serial`, `balloon` for dynamic
  memory, `smb` for mounts, `qemu` with `--gui`); `winvm logs serial -f` follows one.
  A build VM is an instance too: `winvm -i _install-base logs serial -f`.
* **A build seems stuck**: Windows Setup prints nothing of its own; winvm prints a line
  every minute with the time and how much was written, and says so when nothing new
  was written for 15 minutes. Look at the screen: `winvm -i _install-base screenshot`,
  or the VNC address it printed. `WINVM_DEBUG=1 winvm build` keeps a WinPE failure on
  screen instead of powering off.
* **An interrupted first build** starts the Windows install over (downloads resume).
* **`no access to /dev/kvm`**: `sudo usermod -aG kvm $USER`, then log out and in again.
* **A command fails because of an image file**: `winvm images` lists broken files with
  the reason; other images and instances keep working.
* **Disk full**: `winvm df` shows what takes the space; `winvm prune` (then `--cache`,
  `--iso`) frees it. winvm checks free space before a download or build.

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

Progress lines stream from the build VM's serial port from step 3 on; before that
(step 1 can take tens of minutes) winvm prints a line a minute with the time and the
data written. Inside the guest the logs are in `C:\Windows\Setup\winslim*.log`. `WINVM_DEBUG=1 winvm build` makes a
WinPE failure stay on screen (`winvm -i _install-NAME screenshot`) instead of
powering off.

## What to expect

Measured with Windows 11 Pro 25H2, 4 vCPUs / 4 GB, KVM, on an SSD; times vary with the
host, so only the relations are worth remembering:

* **First build** of `base`: up to about an hour, plus downloading about 8.6 GB.
  A `slim` image's first build captures the debloated image too and takes a bit
  longer; after that, rebuilds of it skip the debloat and take a fraction of the time.
* **An image `from` another**: about a minute plus whatever its steps do.
* **Daily use**: an instance boots to SSH in seconds; `winvm sh` commands return in
  well under a second.
* **Disk**: the base layer is about 5.5 GB (Microsoft's ISO: 7-9 GB); a fresh
  instance disk is under 1 MB and grows with what you change; a cached slim image is
  about 6 GB.
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
at the same time needs a license for each: give each its own settings file (`-f` or
`$WINVM_CONFIG`) with its own `uuid` and `data_dir`.

To activate, run this in the guest (`winvm sh`) with your own key. `slmgr` doesn't work
here: the debloat removes VBScript, so `.vbs` scripts don't run.

```powershell
$svc = Get-CimInstance SoftwareLicensingService
Invoke-CimMethod -InputObject $svc -MethodName InstallProductKey -Arguments @{ProductKey='XXXXX-XXXXX-XXXXX-XXXXX-XXXXX'}
Invoke-CimMethod -InputObject $svc -MethodName RefreshLicenseStatus | Out-Null
$win = "ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f' and PartialProductKey is not null"
Get-CimInstance SoftwareLicensingProduct -Filter $win | Invoke-CimMethod -MethodName Activate
(Get-CimInstance SoftwareLicensingProduct -Filter $win).LicenseStatus    # 1 = activated
```

The key must match the image's edition: `edition:` in its file, Windows 11 Pro by default.

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
  edit `src/winvm/data/guest/lists/*.txt` in a checkout of
  <https://github.com/shadymeowy/winvm> and install it with `uv tool install -e .`
  (editable); `winvm build base` then installs with the new lists.
