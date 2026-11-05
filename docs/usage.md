# Using winvm

[Back to the README](../README.md)

- [Commands](#commands)
- [Running commands in the guest](#running-commands-in-the-guest)
- [Mounts](#mounts)
- [Instances and their settings](#instances-and-their-settings)
- [Dynamic memory](#dynamic-memory)
- [Connecting: SSH, RDP, VNC](#connecting-ssh-rdp-vnc)
- [Shell completion](#shell-completion)
- [Where files live](#where-files-live)

## Commands

Instance commands act on `-i NAME`, and the default is `main`. You can put it before or
after the command (`winvm sh -i dev`, `winvm -i dev sh`), except for `ssh` and `cp`, where
it must come first because ssh and scp have their own `-i`.

Wherever an IMAGE is asked for, a name or the path of an image file works.

Deleting something asks first in a terminal. `-y` doesn't ask, and from a script or a
pipe winvm goes ahead without asking. `-y` also skips the question before downloads.

**Run instances**

| Command | What it does |
|---|---|
| `up [NAME\|FILE...] [--image IMAGE] [--ram R\|MIN-MAX\|default] [--cpus N\|default] [--iso ISO] [--all] [--gui]` (`start`) | Create (building images as needed) and start. Ready when SSH answers. |
| `stop [NAME...] [--all] [--timeout S]` (`down`) | ACPI shutdown, all at once; hard power-off after 120 s |
| `restart [NAME...]` | Stop, then start |
| `kill [NAME...]` | Power off at once; unsaved work in the guest is lost |
| `ps [-q] [--json]` (`status`) | List instances: state, ports, disk, RAM, image version, mounts |
| `info` | One instance in detail |
| `wait [SECONDS]` | Wait until SSH answers (default 600 s) |

**Use them**

| Command | What it does |
|---|---|
| `sh [-t] [CMD]` (`shell`) | PowerShell, or run a command ([below](#running-commands-in-the-guest)) |
| `ssh` | Plain ssh: `winvm ssh -L 8080:localhost:80`, `winvm ssh hostname` |
| `cp` | Plain scp: `winvm cp a.txt :C:/Users/user/` |
| `mount [PATH [NAME\|WINPATH]] [--ro] [--save]` | Show a host folder in the guest, or list mounts ([below](#mounts)) |
| `umount NAME\|WINPATH [--save]` | Remove a mount |
| `rdp` | FreeRDP session |
| `gui` | VNC viewer on a running instance, or start it with a GTK window |
| `ga` | Guest agent, works without network: `winvm ga exec PROG [ARGS]` |
| `balloon [SIZE]` | Show or set the guest's RAM while it runs |
| `screenshot` | Save a PNG of the screen |
| `logs [serial\|balloon\|smb\|qemu] [-f]` | An instance's logs; without a name, where they are |
| `cmdline` | Print the QEMU command line |

**Images and disk**

| Command | What it does |
|---|---|
| `build [IMAGE...] [--force] [--gui]` | Build missing or changed images (default: all known). Asks before reinstalling Windows. |
| `images [--json]` | Built versions, status, size, base, users, defining file |
| `save NAME` | Freeze the instance's disk as image NAME; the instance continues on it |
| `reset [IMAGE]` | Discard the instance's changes: fresh disk on the current version of its image, or on IMAGE |
| `rm [NAME...]` | Delete instances: their disks and settings. Images stay. |
| `rmi IMAGE` | Delete an image; refused while anything uses it |
| `prune [-n] [--cache] [--iso]` | Delete unused versions, stale slim images and leftovers. `--cache`: downloads and slim images. `--iso`: ISOs. `-n`: only show. |
| `df` | Disk space used by images, instances, ISOs, cache; free space |

**Set up and inspect**

| Command | What it does |
|---|---|
| `check` | What the host has and lacks, what will be downloaded |
| `fetch [IMAGE...]` | Download now what building needs |
| `editions [IMAGE\|ISO]` | Editions in an image's Windows ISO, or in an ISO file |
| `completion bash\|fish` | Print a shell completion script |
| `config dump [base]` | Built-in settings, or the built-in `base` |
| `config show [IMAGE]` | What is in effect |
| `config path` | Files and folders in use |

`winvm sh` (or `ssh`, `cp`, `rdp`) creates `main` when it doesn't exist yet, so
`winvm sh` alone works from scratch. Other instance names have to be created with `up`
first. winvm refuses to run as root.

## Running commands in the guest

```sh
winvm sh                          # interactive shell (leave with `exit`)
winvm sh ipconfig /all            # a command line; PowerShell parses it
winvm sh 'gci C:\ | select Name'  # one quoted argument = PowerShell code as is
winvm sh notepad "a b.txt"        # words with spaces stay one argument
winvm sh -t python                # -t: a terminal, for interactive programs
cat x | winvm sh findstr foo      # stdin reaches native programs...
cat x | winvm sh 'stdin | sort'   # ...and PowerShell code reads it with `stdin`
winvm sh ipconfig | grep IPv4     # output has Unix line endings
winvm sh cmd /c exit 3; echo $?   # 3: the exit code is the command's (fish: $status)
cd ~/Projects/x && winvm sh -i dev dir    # inside a mount: runs in C:\mnt\projects\x
```

Only use `-t` for programs that need a terminal: tables from `select` get lost with it,
and its output isn't meant for pipes.

Run from inside a mount whose guest folder can't be entered, `winvm sh CMD` runs nothing and exits
1, so a command never runs in the wrong place. With no command, you still get a shell, in your home
folder, with a warning.

Commands run over SSH, which has no desktop, so `winget` and programs that store credentials with
DPAPI won't work in `winvm sh`. Use `winvm rdp` or `winvm gui` for those.

The command travels base64-encoded, so no quoting is lost between your shell, ssh, sshd
and Windows. All commands to an instance share one SSH connection.

## Mounts

```sh
winvm mount ~/data                # running instance: guest C:\mnt\data, at once
winvm mount ~/data d2 --ro        # another name (C:\mnt\d2), read-only
winvm mount ~/src 'C:\src'        # a Windows path instead of a name: the link goes there
winvm mount ~/data --save         # also at every later start of this instance
winvm mount                       # list
winvm umount d2                   # by name, or where it is: winvm umount 'C:\src'
```

Mounts from `run:` settings are set up at every start. `mount` and `umount` change a
running instance until it stops, or for good with `--save`. `--save` also works on a
stopped instance, and `umount --save` drops even a mount the image's `run:` adds.

Each instance runs its own unprivileged Samba server, reached by the guest at
`\\10.0.2.4` through QEMU. The guest gets folder links rather than drive letters,
because a drive letter exists only in the logon session that mapped it.

- winvm only ever removes links it made itself; the guest keeps a list of them.
- A mount point taken by a real folder or file is refused, never replaced.
- A mount the guest can't reach is reported and undone.
- Adding or removing a mount briefly restarts the Samba server, and Windows reconnects
  by itself.

## Instances and their settings

`winvm up NAME` runs the instance NAME, on the image of the same name if there is one (a
saved one too), else on `base`, or on `--image`.

Its settings come from, in order:

1. the machine's `run` defaults,
2. the `run:` of its image and the images below it,
3. its own: `--ram`, `--cpus` and `mount --save`, kept in its folder.

`winvm info` shows them, and `--ram default` drops an instance's own value.

**The host RAM guard.** Before starting a VM, winvm checks `MemAvailable` minus what
running VMs may still grow into. It refuses when the VM plus `settings.mem_reserve` doesn't
fit: stop another instance, or give this one less (`winvm up --ram 3G`, or `--ram 2G-6G`
for dynamic memory).

## Dynamic memory

```yaml
run:
  ram: {min: 2G, max: 8G}            # or: winvm up --ram 2G-8G
```

The VM gets `max`, and winvm keeps it near what the guest actually uses. A small helper
per instance reads the guest's memory stats (virtio-balloon) and moves the balloon:

- it grows the guest at once when free memory runs short;
- it gives memory back to the host after it has been free for half a minute, step by step;
- the guest also gets a pagefile (added on its first start in this mode, one reboot), which
  absorbs a sudden large allocation while memory flows back in;
- if the helper stops or the guest's stats go silent, the guest gets its full `max` back,
  and `winvm up` or `sh` restart a missing helper.

`winvm ps` shows the current size (`2.1/8G`), and `winvm logs balloon` shows the log.

Windows touches all of its RAM at boot, so without this a 4 GB instance takes 4 GB of the
host for as long as it runs. The RAM guard still counts `max` when starting VMs: dynamic
memory leaves room for the host, but it doesn't fit more VMs than `max` allows.

Several instances on the same image can also share identical memory pages with the kernel's
KSM. It's off by default, and `winvm check` shows the command to turn it on.

## Connecting: SSH, RDP, VNC

The first instance's addresses are below, and the *n*-th instance adds *n* to each port.
`winvm ps` shows the real ones. All forwards bind to 127.0.0.1, and the guest firewall is
off on purpose.

| | Address | Login |
|---|---|---|
| SSH | `127.0.0.1:2222` | user `user` (the image's `user:`), key `~/.local/share/winvm/keys/id_ed25519` |
| RDP | `127.0.0.1:3390` | user `user`, password `winslim` (the image's `password:`) |
| VNC | `127.0.0.1:5930` | none |

Your own keys in `settings.ssh_pubkeys` (default `~/.ssh/id_ed25519.pub`) are authorized
too, and brought up to date at every start.

For plain `ssh main` or VS Code Remote-SSH, put this at the **top** of `~/.ssh/config`,
before any `Host` line. An instance's entry exists while it runs.

```
Include /run/user/*/winvm-*/*/ssh_config
```

## Shell completion

Commands and options come from winvm's own definitions, so generate this again after an
upgrade. Instance, image and mount names are looked up live when you press Tab.

```sh
winvm completion bash > ~/.local/share/bash-completion/completions/winvm
winvm completion fish > ~/.config/fish/completions/winvm.fish
# zsh: autoload -U bashcompinit && bashcompinit && source <(winvm completion bash)
```

## Where files live

```
~/.config/winvm/winvm.yaml          machine settings (optional)
~/.config/winvm/images/NAME.yaml    your image files (optional)
~/.local/share/winvm/               --data-dir / $WINVM_DATA_DIR / settings.data_dir
  images/base@fb97f6be18d2.qcow2    read-only layers, <name>@<hash>, + .json metadata
  instances/main/disk.qcow2         an instance's own changes, backed by a layer
  iso/  keys/  share/               ISOs, the SSH key the guests trust, the default mount
~/.cache/winvm/                     --cache-dir / $WINVM_CACHE_DIR / settings.cache_dir
  downloads/  slim/  firmware/      pinned downloads, cached slim images, OVMF if downloaded
  build/                            build scratch
$XDG_RUNTIME_DIR/winvm-*/<name>/    while running: pid, ports, sockets, logs, ssh_config, smb/
```

`winvm config path` prints the folders in use. The run folder's name is a hash of the data
folder, so two data folders never mix.

**What you can delete.** Once an image is built it stands on its own: `winvm prune --cache
--iso` removes the slim images, downloads and ISOs, and images and instances keep working,
frozen as they are. Only building an ISO image again needs an ISO. winvm can download its
pinned one, and `prune --iso` says which ISOs those are. Without the slim cache that build
takes longer.

**Don't delete `images/` or `keys/` by hand.** `images/` holds the images themselves
(`winvm rmi` asks first), and `keys/` holds the key winvm reaches the guests with.

Backing files are relative paths, so `qemu-img info --backing-chain` shows the same chain as
`winvm images` and the data folder can be moved.
