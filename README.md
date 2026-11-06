# winvm

[![PyPI](https://img.shields.io/pypi/v/winvm)](https://pypi.org/project/winvm/)
[![License: MIT](https://img.shields.io/pypi/l/winvm)](https://github.com/shadymeowy/winvm/blob/main/LICENSE)

**A slim Windows 11 VM on Linux, one command away.**

winvm downloads Windows from Microsoft, installs it unattended, strips it down, and
lets you use it like a local command.

```console
$ winvm up                  # the first time: downloads and installs Windows
$ winvm sh hostname         # run a command in it
WINSLIM
$ winvm sh                  # open PowerShell
$ winvm rdp                 # open the desktop
$ winvm stop
```

- **Hands-off.** One command takes you from nothing to a running VM, with no clicking
  through Setup.
- **Small and fast.** About 5.5 GB on disk, seconds to boot, and commands return in
  well under a second.
- **Feels local.** `winvm sh` keeps exit codes, stdin and pipes, and your host folders
  can appear inside the guest.
- **Throwaway.** VMs run on thin layers, so you can snapshot them, start over, or run
  several at once.
- **Defined in YAML.** Describe an image in a small file, like a Dockerfile, and share
  it.
- **No fuss.** It runs as your user, on plain QEMU/KVM, without libvirt or root.

> [!WARNING]
> **This Windows is stripped for development and testing, so don't use it as your
> daily computer.** Windows Update and Defender are off, the firewall is off, there is
> no browser, and the password is public. It's reachable only from your own machine
> (ports bind to `127.0.0.1`), and it isn't activated.
> [What's removed and how to bring it back](https://github.com/shadymeowy/winvm/blob/main/docs/windows.md)

## Install

You need Linux x86-64 with KVM, Python 3.10 or newer, at least 8 GB of RAM and about
25 GB of free disk. [Without KVM](https://github.com/shadymeowy/winvm/blob/main/docs/usage.md#without-kvm)
winvm can emulate instead, which is far slower.

```sh
# 1. QEMU
sudo dnf install qemu-kvm                      # Fedora
sudo apt install qemu-system-x86 qemu-utils    # Debian / Ubuntu
sudo pacman -S qemu-base                       # Arch

# 2. winvm
pipx install winvm                             # or: uv tool install winvm

# 3. See what your machine has and what's missing
winvm check
```

Some features need one more program: shared folders need `samba`, `winvm rdp` needs
FreeRDP, and `winvm gui` needs a VNC viewer. `winvm check` lists them, and winvm's
errors say what to install on your distro.

## Your first VM

```sh
winvm up
```

winvm lists what it needs to download and asks once: about 8 GB, nearly all of it the
Windows 11 ISO, straight from Microsoft and checked by its sha256. Then it installs
Windows by itself.

**The first build can take up to an hour**, depending on your disk and CPU, plus the
download. You can leave it running. It only happens once, and after that `winvm up`
takes seconds.

Want another language, or already have an ISO? [Download one from
Microsoft](https://www.microsoft.com/software-download/windows11) and pass it in. winvm
remembers it:

```sh
winvm up --iso ~/Downloads/Win11_xxx.iso
```

To log in on the desktop, use user `user` and password `winslim`. SSH logs in with a key
that winvm sets up for you.

## Everyday use

| I want to... | Command |
|---|---|
| Start the VM | `winvm up` |
| Open PowerShell in it | `winvm sh` |
| Run one command | `winvm sh ipconfig /all` |
| Run PowerShell code | `winvm sh 'gci C:\ \| select Name'` |
| Use it in a pipeline | `winvm sh ipconfig \| grep IPv4` |
| Open the desktop | `winvm rdp` or `winvm gui` |
| Run something that needs a desktop | `winvm sh --desktop CMD` |
| Copy a file in | `winvm cp a.txt :C:/Users/user/` |
| Forward a port | `winvm ssh -L 8080:localhost:80` |
| See what's running | `winvm ps` |
| Stop it | `winvm stop` |
| Go back to clean Windows | `winvm reset` |

Some things need a logged-on Windows session, which `winvm sh` doesn't have: `winget`, stored
credentials, automating Word or Excel. `winvm up --desktop` logs the VM on, without opening a
window on your screen, and `winvm sh --desktop` runs a command there.
[How that works](https://github.com/shadymeowy/winvm/blob/main/docs/usage.md#commands-that-need-a-desktop)

## Share a folder

Host folders show up in the guest as `C:\mnt\<name>`, at once, while the VM runs:

```sh
winvm mount ~/data            # in the guest: C:\mnt\data
winvm mount ~/src 'C:\src'    # or anywhere you like
winvm mount ~/data --save     # and at every later start
winvm umount data
```

Inside a shared folder, `winvm sh` runs in the matching guest folder, so
`cd ~/data && winvm sh dir` lists the same files. Mounts need `samba` on the host.

## Snapshots and more VMs

Your VM keeps everything across stop and start. To freeze a state and come back to it:

```sh
winvm stop                    # save needs the VM stopped
winvm save golden             # freeze its disk as an image called "golden"
winvm up                      # carry on, then later...
winvm stop
winvm reset                   # ...drop everything since the save
```

Each VM (an *instance*) has a name, and the default one is `main`. To start another one:

```sh
winvm up work --image golden  # a second VM, starting from "golden"
winvm sh -i work              # -i picks which one to use
winvm stop work
winvm rm work                 # delete it; images stay
```

## Build your own image

To put your tools into a VM you can rebuild at any time, write a small YAML file. Its
file name becomes the image's name:

```yaml
# ~/.config/winvm/images/dev.yaml
from: base
steps:
  - download:
      url: https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe
      sha256: 6b33fa9a439a86f553f9f60e538ccabc857d2f308bc77c477c04a46552ade81f
  - run: .\python-3.13.1-amd64.exe /quiet InstallAllUsers=1 PrependPath=1 | Out-Null
run:
  ram: 8G
  mounts:
    projects: ~/Projects      # in the guest: C:\mnt\projects
```

```sh
winvm up dev                  # builds the image if needed, then starts instance "dev"
```

Steps run as an administrator with network access, and a build on top of `base` takes
about a minute plus whatever your steps do. Only what they change is stored, compressed. If a
step needs a logged-on desktop, add `desktop: true`; for instances that should log on by
themselves, add `desktop: true` under `run:`.
[Everything about images](https://github.com/shadymeowy/winvm/blob/main/docs/images.md)

## Free up disk space

```sh
winvm df                      # what takes the space
winvm prune                   # unused image versions and leftovers
winvm prune --cache --iso     # also downloads and ISOs (-n: only show what would go)
```

Built images and instances keep working after that. You only need an ISO again to
reinstall Windows, and `prune --iso` tells you whether winvm can download it again.

## Troubleshooting

- **`no access to /dev/kvm`:** run `sudo usermod -aG kvm $USER`, then log out and back in.
- **Something might hang:** add `--timeout 5m` right after the command's name. It gives up then and
  exits 124. [All the time limits](https://github.com/shadymeowy/winvm/blob/main/docs/usage.md#time-limits)
- **A build seems stuck:** Windows Setup is quiet, so winvm prints a line every minute
  and warns if nothing has been written for 15 minutes. To see the screen, run
  `winvm -i _install-base screenshot` or open the VNC address it printed.
- **A build was interrupted:** run it again. The Windows install starts over, but
  downloads resume.
- **Not enough memory to start:** stop another VM or ask for less, with
  `winvm up --ram 3G`. A range like `--ram 2G-6G` makes it give memory back when idle.
- **An image file has a mistake:** `winvm images` lists it as broken and says why. Other
  images keep working.
- **Disk full:** see [Free up disk space](#free-up-disk-space). winvm checks free space
  before a download or a build.
- **Something else:** `winvm logs` shows an instance's logs, and
  `winvm logs serial -f` follows one. A build VM is an instance too:
  `winvm -i _install-base logs serial -f`.

## Documentation

- **[Using winvm](https://github.com/shadymeowy/winvm/blob/main/docs/usage.md):** all
  commands, `sh`, mounts, dynamic memory, SSH/RDP/VNC, shell completion, where files live
- **[Images](https://github.com/shadymeowy/winvm/blob/main/docs/images.md):** image
  files, steps, rebuilds, settings
- **[Windows in the VM](https://github.com/shadymeowy/winvm/blob/main/docs/windows.md):**
  what's removed, activation, Office, turning updates back on
- **[How it works](https://github.com/shadymeowy/winvm/blob/main/docs/internals.md):**
  the build process, measurements, design notes

## Upgrade and uninstall

**Upgrade** with `pipx upgrade winvm` (or `uv tool upgrade winvm`). Your images and
instances stay, and Windows is never reinstalled unless you ask. To install from a checkout
of the [source](https://github.com/shadymeowy/winvm), use `uv tool install .` or
`pipx install .`.

**Uninstall** with `winvm stop --all`, then `pipx uninstall winvm` (or
`uv tool uninstall winvm`). Then delete what it made: `~/.local/share/winvm` (images,
instances, ISOs), `~/.cache/winvm`, `~/.config/winvm` and any shell completion file.

## License

winvm is MIT licensed. Windows is Microsoft's software under Microsoft's license terms,
and winvm doesn't include or activate it: activating it needs a license of your own.
