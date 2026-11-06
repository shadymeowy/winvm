# Using winvm

[Back to the README](../README.md)

- [Commands](#commands)
- [Running commands in the guest](#running-commands-in-the-guest)
- [Commands that need a desktop](#commands-that-need-a-desktop)
- [Mounts](#mounts)
- [Instances and their settings](#instances-and-their-settings)
- [Dynamic memory](#dynamic-memory)
- [Without KVM](#without-kvm)
- [Time limits](#time-limits)
- [Connecting: SSH, RDP, VNC](#connecting-ssh-rdp-vnc)
- [Shell completion](#shell-completion)
- [Where files live](#where-files-live)

## Commands

Instance commands act on `-i NAME`, and the default is `main`. You can put it before or
after the command (`winvm sh -i dev`, `winvm -i dev sh`), except for `ssh` and `cp`, where
it must come first because ssh and scp have their own `-i`.

Wherever an IMAGE is asked for, a name or the path of an image file works.

In a terminal, winvm asks before deleting something, downloading, or rebooting a running VM to
log on, and `-y` answers yes. From a script or a pipe, it goes ahead with a delete, but refuses a
download or that reboot unless you add `-y`.

**`--timeout TIME`** works on every command and means the same on all of them: give up on the whole
command after this long, and exit 124. It takes `120`, `90s`, `30m` or `2h`, and covers everything
the command does, downloads and starting the VM included. winvm says what it was doing and what it
left behind:

```sh
winvm up --desktop --timeout 5m
winvm cp --timeout 2m big.iso :C:/
winvm build dev --timeout 2h
```

It goes before the command's name or after it. `sh`, `ssh`, `cp`, `rdp` and `ga` pass on what
follows, so there it has to come right after the name: `winvm sh hostname --timeout 5m` would hand
it to `hostname`. `--accel` follows the same rule, and `--rm` always comes right after the name. See
[Time limits](#time-limits) for what happens without it.

**Run instances**

| Command | What it does |
|---|---|
| `up [NAME\|FILE...] [--image IMAGE] [--ram R\|MIN-MAX\|default] [--cpus N\|default] [--desktop [off\|default]] [--rm\|--no-rm] [--iso ISO] [--all] [--gui]` (`start`) | Create (building images as needed) and start. Ready when SSH answers, and in desktop mode once logged on. |
| `stop [NAME...] [--all] [--kill-after TIME]` (`down`) | ACPI shutdown, all at once; hard power-off after 2 minutes (10 when emulating), then exit 3. Deletes a temporary instance. |
| `restart [NAME...] [--kill-after TIME]` | Stop, then start |
| `kill [NAME...]` | Power off at once; unsaved work in the guest is lost. Deletes a temporary instance. |
| `ps [-q] [--json]` (`status`) | List instances: state, ports, disk, RAM, image version, mounts, whether temporary |
| `info` | One instance in detail |
| `wait [TIME]` | Wait until SSH answers: exit 124 after TIME (default 10 minutes) |

**Use them**

| Command | What it does |
|---|---|
| `sh [-t] [--desktop] [--rm] [--timeout TIME] [CMD]` (`shell`) | PowerShell, or run a command ([below](#running-commands-in-the-guest)) |
| `ssh [--rm]` | Plain ssh: `winvm ssh -L 8080:localhost:80`, `winvm ssh hostname` |
| `cp` | Plain scp: `winvm cp a.txt :C:/Users/user/` |
| `mount [PATH [NAME\|WINPATH]] [--ro] [--save]` | Show a host folder in the guest, or list mounts ([below](#mounts)) |
| `umount NAME\|WINPATH [--save]` | Remove a mount |
| `rdp [--rm]` | FreeRDP session |
| `gui [--rm]` | VNC viewer on a running instance, or start it with a GTK window |
| `ga` | Guest agent, works without network: `winvm ga exec PROG [ARGS]` |
| `balloon [SIZE]` | Show or set the guest's RAM while it runs |
| `screenshot [FILE]` | Save a PNG of the screen |
| `logs [serial\|balloon\|smb\|qemu] [-f]` | An instance's logs; without a name, where they are |
| `cmdline [--gui]` | Print the QEMU command line |

**Images and disk**

| Command | What it does |
|---|---|
| `build [IMAGE...] [--force] [--gui]` | Build missing or changed images (default: all known). Asks before reinstalling Windows. |
| `images [--json]` | Built versions, status, size, base, users, defining file |
| `save NAME` | Freeze the (stopped) instance's disk as image NAME; the instance continues on it |
| `reset [IMAGE]` | Discard the (stopped) instance's changes: fresh disk on the current version of its image, or on IMAGE |
| `rm [NAME...]` | Delete stopped instances: their disks and settings. Images stay. |
| `rmi IMAGE` | Delete an image; refused while anything uses it |
| `prune [-n] [--cache] [--iso]` | Delete unused versions, stale slim images, stopped temporary VMs and leftovers. `--cache`: downloads and slim images. `--iso`: ISOs. `-n`: only show. |
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

`winvm sh` (or `ssh`, `cp`, `rdp`, `gui`) creates `main` when it doesn't exist yet, and starts an
instance that's stopped, so `winvm sh` alone works from scratch. `winvm -i office sh` does the same
when there's an image called `office`. Any other name has to be created with `up` first, so a
mistyped one can't start a build. winvm refuses to run as root.

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
winvm sh --timeout 5m '.\slow.exe'  # give up after 5 minutes: ends it, exit 124
winvm sh --desktop '.\needs-a-desktop.exe'  # see below
```

Only use `-t` for programs that need a terminal: tables from `select` get lost with it,
and its output isn't meant for pipes.

Run from inside a mount whose guest folder can't be entered, `winvm sh CMD` runs nothing and exits
1, so a command never runs in the wrong place. With no command, you still get a shell, in your home
folder, with a warning.

**Large stdin.** PowerShell code should read it with `stdin`, which handled 3 MB in testing.
`[Console]::In` and `[Console]::OpenStandardInput()` can stall the whole connection from around
16 KB. A native program gets stdin directly, which also handled 3 MB when it prints little. One
that prints a lot while it reads usually stalls from about 256 KB, and the connection is lost with
exit 255. For that, go through PowerShell with `winvm sh 'stdin | findstr "^"'`, or copy the data
in with `winvm cp`.

On `winvm sh`, `--timeout` also ends the command in the guest and what it started, and says if it
couldn't. It doesn't end what Windows started for it, such as Store apps and COM servers. It needs
a command, and doesn't go with `-t`.

The command travels base64-encoded, so no quoting is lost between your shell, ssh, sshd
and Windows. All commands to an instance share one SSH connection.

## Commands that need a desktop

What runs over SSH has no desktop (session 0) and, because SSH logs in with a key, can't use
DPAPI, the Windows store for credentials. So `winget`, programs that store credentials and much
COM automation, like exporting from Word, fail or hang there. For those, ask for the logged-on
session:

```sh
winvm up work --desktop                  # returns once it is logged on
winvm sh -i work --desktop 'winget install 7zip.7zip --accept-source-agreements --accept-package-agreements'
```

**Nothing appears on your screen.** The desktop exists inside the VM, and you only see it if you
ask to:

| | Window on your screen | Logged-on session |
|---|---|---|
| `winvm up` | no | no |
| `winvm up --desktop` | no | yes |
| `winvm up --gui` | yes | no |
| `winvm up --desktop --gui` | yes | yes |

The window opens only when this starts the VM. On a running one, use `winvm gui`.

**Desktop mode** makes the VM log on by itself at every boot. The first start in that mode reboots
once, which adds about 30 seconds. Being logged on costs about 560 MB of the guest's RAM. Set it
for one instance, or in an image file for every instance of that image and the images on it:

```sh
winvm up work --desktop                  # keep it for this instance
winvm up work --desktop off              # this instance: no, whatever its image says
winvm up work --desktop default          # back to what its image says
```

```yaml
run:
  desktop: true
```

`winvm sh` keeps using SSH, as before, unless you add `--desktop`. Then it depends on the VM:

| The VM is... | `winvm sh --desktop CMD` |
|---|---|
| logged on (also by hand, through `gui` or `rdp`) | runs it |
| in desktop mode, still booting | waits for the logon, then runs it |
| stopped | starts it, logs on (rebooting once if it wasn't in desktop mode), runs it |
| running, not logged on, in a terminal | asks whether to reboot it |
| running, not logged on, from a script | refuses, unless you add `-y` |

winvm never reboots a running VM without asking, because that could interrupt what it's doing.
`winvm up --desktop` follows the same rule. Once it has rebooted to log on, the instance stays in
desktop mode.

The command runs as the same elevated user, and returns its exit code. A few things differ from
plain `winvm sh`:

- It takes about 2.3 seconds to start, against 0.6.
- Output is text, with stderr mixed into stdout.
- Stdin isn't passed on, so piping data into it is an error. Copy the data in with `winvm cp`. If
  the command doesn't need any but your script's stdin has some, add `</dev/null`. winvm looks for
  0.1 seconds before it starts; for data that comes later it only warns, after the command.
- Nobody can answer a prompt, so `Read-Host` fails instead of hanging. A window that waits for a
  click does hang, so use `--timeout`: it fails with a picture of the screen, whose path it prints.
- Programs that wait for their window to close, like `notepad`, keep the command waiting. Use
  `Start-Process` to leave them open.
- `-t` doesn't apply, and neither does a shell without a command: use `winvm gui` or `winvm rdp`.

**For a script,** this runs one export from start to finish and always stops the VM afterwards,
even one that was already running. `winvm cp` doesn't create folders, so `C:\work` has to exist
already, from the image or an earlier step:

```sh
#!/bin/sh
set -e
winvm up --desktop                       # returns once logged on
trap 'winvm stop' EXIT                   # after up: a refused up shouldn't stop a running VM

winvm cp report.docx :C:/work/report.docx
winvm sh --desktop --timeout 5m 'powershell -ExecutionPolicy Bypass -File C:\work\export.ps1'
winvm cp :C:/work/report.pdf ./report.pdf
```

For Office automation, have your script call `$word.Quit()`, because Windows starts Word for COM
and it may outlive a command that gets ended.

**The password.** Windows logs on with a password stored in the registry, in plain text, while
desktop mode is on. winvm stores what it replaces, and when desktop mode goes off it puts that back
and removes the password. `winvm save` copies whatever is on the disk into the image, password
included. An instance started from that image without desktop mode logs on once and then removes it.

winvm uses the password the image was built with. If you changed it, winvm stops before rebooting
and asks for the current one, under `run:` in the image file or `~/.config/winvm/winvm.yaml`:

```yaml
run:
  desktop: true
  password: the-current-password
```

winvm doesn't reset the password itself, because that can make data protected by DPAPI unreadable.
A logon notice is cleared while desktop mode is on, because it would wait for someone to press OK.

**Looking at it.** `winvm gui` only watches the screen, so it can't disturb the session. `winvm rdp`
takes the session away from the VM's own screen, which then shows the login screen, also after you
disconnect. The session stays logged on and `--desktop` commands keep working, but `winvm gui`
and the pictures from `--timeout` only show that login screen until the next reboot. A program
that needs a visible screen may not work during that time either, so prefer `winvm gui` to look.

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
3. its own: `--ram`, `--cpus`, `--desktop` and `mount --save`, kept in its folder.

`winvm info` shows them, and `--ram default` drops an instance's own value. `reset` keeps
them, and sets up desktop mode again on the fresh disk.

**Temporary VMs.** `--rm` on `sh`, `ssh`, `rdp` or `gui` starts a fresh VM on an image and deletes it
when the command ends, also after Ctrl-C or `--timeout`, or when you close the `rdp` or `gui` window:

```sh
winvm sh --rm -i office 'C:\tools\test.exe'   # a fresh VM on image office, gone afterwards
winvm -i office ssh --rm hostname              # for ssh, -i goes first
winvm rdp --rm -i office                       # gone when the window closes
```

Once the image is built, it takes about 14 seconds to start, or about 45 with `--desktop`. The
instance has the image's name, so `--rm` refuses if a real instance of that name exists, and only one
can run per name. While it runs, other commands with that `-i` use the same VM.

**For several commands,** use `up --rm`. `stop` or `kill` then deletes the VM. `restart` doesn't. In a
script, set a `trap`, or a failing step leaves it running:

```sh
winvm up work --image office --rm
trap 'winvm stop work' EXIT
winvm -i work cp test.exe :C:/Users/user/
winvm sh -i work 'C:\Users\user\test.exe'
```

**If nothing was there to delete it,** because Windows shut down by itself, its window was closed or
winvm was killed, the VM stays behind, and `winvm ps` marks it as temporary. `stop` and `kill` delete
it. Once it's off:

- `up` replaces it with a fresh VM on the same image, temporary again if it came from `up --rm`;
- `sh`, `ssh`, `cp`, `rdp` and `gui` delete it, then carry on as if it had never been there;
- `prune` deletes it;
- `reset` keeps it temporary, and `save` freezes what's in it as an image, which stays.

One from a killed `--rm` command that's still running is replaced by the next `--rm` of that name. If
`stop --timeout` runs out, the VM keeps shutting down, and is deleted in the same way.

**To make this the default,** set it under `run:` in an image file, which images on it inherit, or in
`~/.config/winvm/winvm.yaml` for every image:

```yaml
run:
  rm: true
```

Then new instances are temporary without asking: `sh`, `ssh`, `rdp` and `gui` delete theirs at the end,
and `stop` deletes one from `up`. `winvm up NAME --no-rm` makes one that stays, and an instance that
already exists is left alone. `cp` refuses when nothing is running, because the VM would be gone right
after the copy, so start one with `up` first. A saved image has no file, so for one of those, write a
small file with `from: NAME` and this setting.

**The host RAM guard.** Before starting a VM, winvm checks `MemAvailable` minus what
running VMs may still grow into. It refuses when the VM plus `settings.mem_reserve` doesn't
fit: stop another instance, or give this one less (`winvm up --ram 3G`, or `--ram 2G-6G`
for dynamic memory). A build VM uses the machine's `ram` (4 GB unless you changed it), whatever
the image's `run: ram` says.

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

## Without KVM

winvm needs KVM to be fast. If you only lack access to it, fix that: run
`sudo usermod -aG kvm $USER`, then log in again. If the machine has no KVM at all, for example
inside a VM or a CI runner, QEMU can emulate the CPU. You have to ask for it, because of what it
costs:

```sh
winvm up --accel tcg              # this command only
export WINVM_ACCEL=tcg            # this shell
```

```yaml
# ~/.config/winvm/winvm.yaml: this machine, always
settings:
  accel: tcg
```

Measured on one 16-thread desktop with 4 vCPUs and 4 GB:

| | KVM | Emulated |
|---|---|---|
| `winvm up` | about 13 s | about 3.5 minutes |
| `winvm sh hostname` | 0.6 s | about 7.5 s |
| CPU-heavy PowerShell | | about 25 times slower |
| Clean `winvm stop` | about 10 s | about 16 s |
| An image `from` another, one small step | about 1 minute | about 4.5 minutes |

**A full Windows install under emulation hasn't been run.** It starts, and begins writing Windows
to the disk after about 4 minutes. It takes up to an hour on KVM, so expect many hours. If you can,
build the image once on a machine with KVM.

**Desktop mode is poor when emulated.** `winvm up --desktop` took about 10 minutes, close to the
limit on a logon, so give it a longer `--timeout`. The first command in that session then lost its
connection in both tries, so avoid it there.

`winvm restart` keeps a running VM emulated. Starting a stopped one needs the setting again, which
`$WINVM_ACCEL` or `settings.accel` takes care of. QEMU uses about 700 MB more than the VM's RAM, for
its translated code, and the RAM guard counts on that.

## Time limits

Without `--timeout`, a command that's working has no limit: installing Windows can take its hours,
`winvm sh` waits for its command, and `winvm cp` takes as long as the file does. What winvm waits
for by itself is limited, and it says so when one of these runs out:

| Waiting for | Limit | Then |
|---|---|---|
| SSH to answer, a logon, a reboot, a build VM's shutdown, or winvm's own commands in the guest | 10 minutes each | error, exit 1, or a warning where winvm can carry on, as with setting up mounts |
| A VM to stop | 2 minutes, 10 when emulating; `--kill-after` | hard power-off, exit 3 |
| A shared folder to answer | 30 s, 2 minutes when emulating | warning; `winvm mount` undoes the mount, exit 1 |
| An SSH connection to open | 5 s, 30 s when emulating | that try fails; waits above try again |
| The guest to answer on an open SSH connection | 30-40 s, 3 minutes when emulating, but not for a program that's simply running | connection lost, exit 255, with a warning |
| QEMU's monitor or the guest agent | 30 s for each answer | error (`ga`, `balloon`, `screenshot`, `stop`) |
| A download to send anything | 60 s | error; run it again, it resumes |
| Memory to free up before a start | until it holds level for 5 s | the RAM guard refuses |
| Cleaning up in the guest after a limit ran out | 30 s | says the command may still be running |

`--timeout` overrides the 10 minute waits in both directions: with `--timeout 2h`, `winvm up` waits
up to 2 hours for a slow VM. The dynamic memory helper has its own timings, in
[Dynamic memory](#dynamic-memory), and it logs when it stops. Inside the install, Windows' setup
scripts have a few short retries of their own, which only show if the install fails.

**When `--timeout` runs out,** what was already underway stays as follows:

- `up`, `restart`, `wait`: a VM that was starting keeps starting. `winvm up` finishes setting it up.
- `stop`: the VM keeps shutting down. Add `--kill-after` if it has to be off by then.
- `sh`: the command is ended, or winvm says it couldn't be.
- `--rm`: the VM is deleted, along with whatever was running in it.
- `ssh`, `ga exec`: a command in the guest may go on.
- `cp`: a partial file may be left.
- `mount`: the mount is undone.
- `build`: its scratch VM is stopped, and a `from` image's is removed.

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
