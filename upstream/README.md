# Upstream kernel support

The `px13_tgp_probe` module drives the same WMI device IDs as the kernel's
`asus-armoury` driver (`DGPU_SET_TGP`, `NV_DYN_BOOST`, `NV_THERM_TARGET`,
`PPT_PL1/2/3`). Its attributes return `ENODEV` on `HN7306WV` only because the
driver has no power limit entry for this board. The patch here adds one, with
the AC ranges from [`docs/FINDINGS.md`](../docs/FINDINGS.md). Once it is
merged, stock asusd and ROG Control Center can set these limits, and the
kernel module is no longer needed for them.

Battery (DC) limits are not included because they have not been measured;
without them the attributes return `ENODEV` on battery, as for other boards
that list AC data only.

## Testing on the running kernel

`asus-armoury` is a loadable module, so the patched driver can be built
outside the kernel tree and swapped in. On AC power:

```bash
# sources of the running kernel; for CachyOS use its tag, e.g.
# https://raw.githubusercontent.com/CachyOS/linux/cachyos-7.2.9-2/drivers/platform/x86/
mkdir armoury && cd armoury
for f in asus-armoury.c asus-armoury.h firmware_attributes_class.h; do
    curl -fsSL -o "$f" "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/drivers/platform/x86/$f?h=v$(uname -r | cut -d- -f1)"
done
# apply the hunk from the patch to asus-armoury.h, then:
echo 'obj-m += asus-armoury.o' > Kbuild
make -C /lib/modules/$(uname -r)/build M=$PWD LLVM=1 modules

sudo systemctl stop asusd       # asusd must not restore its values meanwhile
sudo rmmod asus_armoury && sudo insmod ./asus-armoury.ko
grep . /sys/class/firmware-attributes/asus-armoury/attributes/{nv_tgp,nv_dynamic_boost,nv_temp_target,ppt_pl1_spl}/{min,max}_value
sudo rmmod asus_armoury && sudo modprobe asus_armoury
sudo systemctl start asusd
```

The driver writes to the firmware only when an attribute is written, so
loading it and reading the ranges changes nothing.

## Sending

Review the patch, add your sign-off and send it to the x86 platform driver
maintainers and list, with the asus-linux maintainers in copy:

```bash
git am 0001-platform-x86-asus-armoury-add-support-for-HN7306WV.patch   # in a kernel tree
git commit --amend -s --no-edit
git send-email -1 \
    --to="Hans de Goede <hansg@kernel.org>" \
    --to="Ilpo Järvinen <ilpo.jarvinen@linux.intel.com>" \
    --cc="platform-driver-x86@vger.kernel.org" \
    --cc="Luke D. Jones <luke@ljones.dev>" \
    --cc="Denis Benato <denis.benato@linux.dev>"
```

The patch carries `Assisted-by: LLM` as required by
`Documentation/process/coding-assistants.rst`; only the submitter may add
`Signed-off-by`.
