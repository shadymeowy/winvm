# guest/extra: your own additions to the base

Everything in this folder is copied into the guest, and each `*.ps1` runs once
while the base is built:

* in name order (prefix them: `10-7zip.ps1`, `20-python.ps1`, ...)
* as SYSTEM, in this folder, so `.\setup.msi` finds the installer next to it
* after the debloat, before cleanup and trim, so whatever it installs ends up in
  layer `base` and every instance on it
* **offline**: the installer VM has no network (nothing phones home during the
  build), so put every installer the script needs in this folder

Its output and a `WARN` line for a failure or a non-zero exit show up in the
install log. The folder is deleted from the guest afterwards.

Example `10-7zip.ps1`, with `7z2409-x64.msi` next to it:

```powershell
Start-Process msiexec -Wait -ArgumentList '/i', "$PWD\7z2409-x64.msi", '/qn', '/norestart'
```

Wait for installers to finish (`Start-Process -Wait`, or `| Out-Null` for
console programs), or the shrink step will run while they are still going.

Pick up changes:

* `./winvm iso --payload`, then `./winvm clean --base && ./winvm install --standalone`
  (the slim ISO keeps its captured image; ~20 s + ~10 min)
* or `./winvm build && ./winvm clean --base && ./winvm install` (Microsoft ISO, ~22 min)

Scripts are versioned; other files here (installers) are git-ignored.
For things you try out or change often, a saved layer is quicker:
`./winvm save NAME`.
