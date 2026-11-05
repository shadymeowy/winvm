# Images

[Back to the README](../README.md)

An image is one YAML file, and its file name is its name. It's built one of two ways:

- **from an ISO:** an unattended install and debloat, offline;
- **from another image:** boot it, run steps with network, keep the changes.

- [Examples](#examples)
- [Steps](#steps)
- [Where image files come from](#where-image-files-come-from)
- [Rebuilds](#rebuilds)
- [Saved images](#saved-images)
- [Machine settings](#machine-settings)
- [Other Windows versions and languages](#other-windows-versions-and-languages)

## Examples

An image on top of another:

```yaml
# ~/.config/winvm/images/dev.yaml
from: base                           # an image name, or a path: ../shared/base.yaml
steps:                               # with network; files land in a work folder
  - download:
      url: https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe
      sha256: 6b33fa9a439a86f553f9f60e538ccabc857d2f308bc77c477c04a46552ade81f
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

Your own `base`, built from an ISO. It only needs what differs from the built-in one, which
`winvm config dump base` shows:

```yaml
# ~/.config/winvm/images/base.yaml
edition: Windows 11 Pro N            # `winvm editions` lists them
timezone: Pacific Standard Time      # Windows time zone name (`tzutil /l` lists them)
slim: true                           # cache the debloated image: much faster rebuilds
setup:                               # during the install, as SYSTEM, without network
  - download: {url: "https://www.7-zip.org/a/7z2409-x64.msi", sha256: <sha256>}
  - run: Start-Process msiexec -Wait -ArgumentList '/i', '7z2409-x64.msi', '/qn'
```

## Steps

| Step | What it does |
|---|---|
| `run: CODE` | PowerShell. A thrown error or a non-zero exit fails the build. |
| `download: {url, sha256}` | Fetch on the host, check the sha256 (`curl -sL URL \| sha256sum`), put the file in the work folder |
| `copy: [HOST]` | Put a host file or folder in the work folder |
| `copy: [HOST, GUEST]` | Put it anywhere in the guest (`from` images only) |
| `restart: true` | Reboot (`from` images only) |

Run steps start in the work folder, which is deleted when the build ends.

There are two lists of steps, and they differ in who runs them:

| | `steps:` (`from` images) | `setup:` (ISO images) |
|---|---|---|
| **Runs as** | The image's user, an elevated administrator | `SYSTEM` |
| **Runs through** | SSH on a running VM | `SetupComplete`, during the install |
| **Network** | Yes | No |
| **`HKCU`, user profile** | Your user's | The SYSTEM profile's, not your user's |

Prefer `steps` unless you have a reason not to: admin rights are enough for most installers,
and you get network access. There's no interactive desktop in either list, so run installers
silently, or a GUI installer may hang. Files that `setup` steps use travel on a CD: their
names must be valid on Windows and at most 64 characters long.

## Where image files come from

A name like `dev` means `dev.yaml` in `~/.config/winvm/images/` (setting `images_dir`), else
a built-in, of which there is only `base`. Any other file is given by its path once. winvm
remembers it, so later commands, and `from: NAME` in other files, work by name from any
folder.

- **A built image stays usable by name when its file is gone**, like a Docker image. It just
  can't be rebuilt without the file. `prune` keeps its newest version, and `rmi` deletes it.
- **Your `base.yaml` replaces the built-in `base`**, and `winvm images` says so.
- **Two files with the same name are an error**, never a silent pick.
- **A file with a mistake fails only the commands that use it**, and `winvm images` lists it
  as broken.
- **A file's folder is its context:** `copy: [./dotfiles]` and mount paths are relative to
  the file.
- **For files you share, leave `iso:` out:** everyone links their own ISO once with `--iso`.

## Rebuilds

Rebuilds work like Docker layers. Every image has a content hash over its definition, the
files it copies and the image below it, but not over where its file is.

**A `from` image** that changes gets a new version, along with everything built on it, on
the next `build`, `reset`, or `up` of a new instance.

**An ISO image** works differently, because installing Windows again takes up to an hour.
When its file or a winvm upgrade changes it, `winvm images` marks it *outdated*, and it stays
in use until you run `winvm build NAME`, which asks first.

**Existing instances** keep running on the version they were created on. `winvm ps` marks
them outdated, and `winvm reset` moves them over, discarding their changes.

`winvm prune` deletes versions nothing uses.

## Saved images

`winvm save NAME` freezes an instance's disk as an image, for states you produce by hand,
such as after activating something. The instance continues on the saved image, so the image
itself never changes, and `winvm reset` returns to it.

A saved image can't be rebuilt, because there's no recipe, and it stays on the `base` it was
made from. To get something that rebuilds on a newer `base`, write an image file.

## Machine settings

Folders, ports, UUID, RAM reserve and the default `run` are in
`~/.config/winvm/winvm.yaml`, merged over the built-in ones, which `winvm config dump` prints.
They never change what an image contains, so an image file means the same on every machine.

## Other Windows versions and languages

winvm downloads one pinned ISO (`settings.windows`: Windows 11 25H2, English) from Microsoft's
own CDN and checks its sha256. It doesn't script Microsoft's download page or use third-party
mirrors.

For other languages or versions, download the ISO yourself and pass `--iso`, or pin another
one in `settings.windows` (url, sha256 and size). An ISO you bring is identified by its sha256
too, and winvm says whether it matches the one it knows.
