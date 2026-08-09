# Navmesh cache drop-in folder

**You normally do not need this folder.** The Import step downloads the matching
navmesh cache by itself, over plain HTTPS, with nothing to install.

It is here for the offline case: download a `navmesh-cache-*.zip` from the
[releases page](https://github.com/bryantmh/tes4skyrim/releases), drop it in
here, and Import installs it instead of downloading. Useful on a machine with no
internet, or behind a proxy that blocks the download.

Navmesh generation is the slowest part of Import (measured: 192 s -> 3.8 s on
Nehrim). A cache only ever saves time: every entry carries a hash of the inputs
it was built from, so anything that does not match is regenerated and the
converted plugin is identical either way.
