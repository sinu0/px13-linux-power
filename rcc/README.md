# ROG Control Center integration

The power controls appear in ROG Control Center through its tuning provider
support, which lives in a small asusctl fork:
[sinu0/asusctl](https://github.com/sinu0/asusctl), branch `tuning-provider`.
The fork is upstream asusctl plus one feature; its `FORK.md` describes how
to build it and how to update it to a new asusctl release, and
`TUNING_PROVIDER.md` documents the D-Bus API that `px13-powerd` implements.

```bash
git clone -b tuning-provider https://github.com/sinu0/asusctl.git
cd asusctl
cargo build --release -p rog-control-center
sudo install -m755 target/release/rog-control-center /usr/local/bin/rog-control-center
```

The binary in `/usr/local/bin` takes precedence over the packaged one, so
distribution updates of `rog-control-center` do not remove the controls.

Without the fork, everything still works through `px13ctl`.
