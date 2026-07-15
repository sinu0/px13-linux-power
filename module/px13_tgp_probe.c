// SPDX-License-Identifier: GPL-2.0
#include <linux/init.h>
#include <linux/dmi.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_data/x86/asus-wmi.h>

struct px13_wmi_value {
	const char *name;
	u32 id;
};

struct px13_tunable {
	const char *name;
	u32 id;
	int min;
	int max;
	int value;
};

static DEFINE_MUTEX(px13_wmi_lock);

static const struct dmi_system_id px13_dmi_table[] = {
	{
		.ident = "ASUS ProArt PX13 HN7306WV",
		.matches = {
			DMI_EXACT_MATCH(DMI_SYS_VENDOR, "ASUSTeK COMPUTER INC."),
			DMI_EXACT_MATCH(DMI_BOARD_NAME, "HN7306WV"),
		},
	},
	{ }
};
MODULE_DEVICE_TABLE(dmi, px13_dmi_table);

/* Windows ASUS ThrottlePlugin manual-GPU controls, kept read-only here. */
#define PX13_WMI_DEVID_GPU_MANUAL_CORE	0x0012009B
#define PX13_WMI_DEVID_GPU_MANUAL_MEMORY	0x0012009F
#define PX13_WMI_DEVID_GPU_MANUAL_POWER	0x0012009C
#define PX13_WMI_DEVID_GPU_MANUAL_THERMAL	0x0012009D
#define PX13_WMI_DEVID_GPU_MANUAL_MODE2	0x0012009E

static struct px13_tunable gpu_tgp = {
	.name = "gpu_tgp",
	.id = ASUS_WMI_DEVID_DGPU_SET_TGP,
	/* ASUS calls this GPU VAR: extra TGP added to the firmware base. */
	.min = 0,
	/* 55 W base + 40 W variable TGP + 25 W Dynamic Boost = 120 W. */
	.max = 40,
	.value = -1,
};

static int gpu_base_tgp = -1;

static struct px13_tunable platform_mode = {
	.name = "platform_mode",
	.id = ASUS_WMI_DEVID_THROTTLE_THERMAL_POLICY,
	.min = 0,
	.max = 4,
	.value = -1,
};

static struct px13_tunable gpu_dynamic_boost = {
	.name = "gpu_dynamic_boost",
	.id = ASUS_WMI_DEVID_NV_DYN_BOOST,
	.min = 5,
	.max = 25,
	.value = -1,
};

static struct px13_tunable gpu_thermal_target = {
	.name = "gpu_thermal_target",
	.id = ASUS_WMI_DEVID_NV_THERM_TARGET,
	.min = 75,
	.max = 87,
	.value = -1,
};

static struct px13_tunable cpu_spl = {
	.name = "cpu_spl",
	.id = ASUS_WMI_DEVID_PPT_PL1_SPL,
	.min = 5,
	.max = 45,
	.value = -1,
};

static struct px13_tunable cpu_sppt = {
	.name = "cpu_sppt",
	.id = ASUS_WMI_DEVID_PPT_PL2_SPPT,
	.min = 5,
	.max = 55,
	.value = -1,
};

static struct px13_tunable cpu_fppt = {
	.name = "cpu_fppt",
	.id = ASUS_WMI_DEVID_PPT_PL3_FPPT,
	.min = 5,
	.max = 65,
	.value = -1,
};

static const struct px13_wmi_value px13_values[] = {
	{ "gpu_base_tgp", ASUS_WMI_DEVID_DGPU_BASE_TGP },
	{ "gpu_tgp", ASUS_WMI_DEVID_DGPU_SET_TGP },
	{ "platform_mode", ASUS_WMI_DEVID_THROTTLE_THERMAL_POLICY },
	{ "gpu_dynamic_boost", ASUS_WMI_DEVID_NV_DYN_BOOST },
	{ "gpu_thermal_target", ASUS_WMI_DEVID_NV_THERM_TARGET },
	{ "gpu_manual_core", PX13_WMI_DEVID_GPU_MANUAL_CORE },
	{ "gpu_manual_memory", PX13_WMI_DEVID_GPU_MANUAL_MEMORY },
	{ "gpu_manual_power", PX13_WMI_DEVID_GPU_MANUAL_POWER },
	{ "gpu_manual_thermal", PX13_WMI_DEVID_GPU_MANUAL_THERMAL },
	{ "gpu_manual_mode2", PX13_WMI_DEVID_GPU_MANUAL_MODE2 },
	{ "cpu_spl", ASUS_WMI_DEVID_PPT_PL1_SPL },
	{ "cpu_sppt", ASUS_WMI_DEVID_PPT_PL2_SPPT },
	{ "cpu_fppt", ASUS_WMI_DEVID_PPT_PL3_FPPT },
};

static int px13_set_tunable(struct px13_tunable *tunable, int value)
{
	u32 result = 0;
	int err;

	/* Parameter callbacks can run before module_init(), so guard every write. */
	if (!dmi_first_match(px13_dmi_table)) {
		pr_err("px13-tgp: refusing WMI write on an unsupported system\n");
		return -ENODEV;
	}

	if (value < tunable->min || value > tunable->max ||
	    (tunable == &platform_mode && value == 3)) {
		pr_err("px13-tgp: refusing %s=%d; valid range is %d-%d\n",
		       tunable->name, value, tunable->min, tunable->max);
		return -EINVAL;
	}

	mutex_lock(&px13_wmi_lock);
	err = asus_wmi_set_devstate(tunable->id, value, &result);
	mutex_unlock(&px13_wmi_lock);
	if (err) {
		pr_err("px13-tgp: setting %s=%d failed: %d\n",
		       tunable->name, value, err);
		return err;
	}
	if (!result) {
		pr_err("px13-tgp: firmware rejected %s=%d\n",
		       tunable->name, value);
		return -EIO;
	}

	tunable->value = value;
	pr_info("px13-tgp: set %s=%d, firmware result=0x%08x\n",
		tunable->name, value, result);
	return 0;
}

static int px13_param_set(const char *val, const struct kernel_param *kp)
{
	struct px13_tunable *tunable = kp->arg;
	int value;
	int err;

	err = kstrtoint(val, 10, &value);
	if (err)
		return err;
	return px13_set_tunable(tunable, value);
}

static int px13_param_get(char *buf, const struct kernel_param *kp)
{
	const struct px13_tunable *tunable = kp->arg;

	return sysfs_emit(buf, "%d\n", tunable->value);
}

static const struct kernel_param_ops px13_param_ops = {
	.set = px13_param_set,
	.get = px13_param_get,
};

module_param_cb(gpu_tgp, &px13_param_ops, &gpu_tgp, 0600);
MODULE_PARM_DESC(gpu_tgp, "NVIDIA variable TGP added above base (0-40 W)");
module_param(gpu_base_tgp, int, 0400);
MODULE_PARM_DESC(gpu_base_tgp, "Firmware-reported NVIDIA base TGP (read-only)");
module_param_cb(platform_mode, &px13_param_ops, &platform_mode, 0600);
MODULE_PARM_DESC(platform_mode, "ASUS performance mode: 0=Balanced, 1=Turbo, 2=Silent, 4=Manual");
module_param_cb(gpu_dynamic_boost, &px13_param_ops, &gpu_dynamic_boost, 0600);
MODULE_PARM_DESC(gpu_dynamic_boost, "NVIDIA Dynamic Boost allowance (5-25 W)");
module_param_cb(gpu_thermal_target, &px13_param_ops, &gpu_thermal_target, 0600);
MODULE_PARM_DESC(gpu_thermal_target, "NVIDIA GPU thermal target (75-87 C)");
module_param_cb(cpu_spl, &px13_param_ops, &cpu_spl, 0600);
MODULE_PARM_DESC(cpu_spl, "AMD sustained package power limit (5-45 W)");
module_param_cb(cpu_sppt, &px13_param_ops, &cpu_sppt, 0600);
MODULE_PARM_DESC(cpu_sppt, "AMD slow package power limit (5-55 W)");
module_param_cb(cpu_fppt, &px13_param_ops, &cpu_fppt, 0600);
MODULE_PARM_DESC(cpu_fppt, "AMD fast package power limit (5-65 W)");

static void px13_read_values(void)
{
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(px13_values); i++) {
		u32 value = 0;
		int err;

		mutex_lock(&px13_wmi_lock);
		err = asus_wmi_get_devstate_dsts(px13_values[i].id, &value);
		mutex_unlock(&px13_wmi_lock);
		if (err)
			pr_info("px13-tgp: %s (0x%08x) read unavailable: %d\n",
				px13_values[i].name, px13_values[i].id, err);
		else
			pr_info("px13-tgp: %s (0x%08x) raw=0x%08x value=%u\n",
				px13_values[i].name, px13_values[i].id, value,
				value & ~ASUS_WMI_DSTS_PRESENCE_BIT);

		if (!err && px13_values[i].id == ASUS_WMI_DEVID_DGPU_BASE_TGP)
			gpu_base_tgp = value & ~ASUS_WMI_DSTS_PRESENCE_BIT;
	}
}

static int __init px13_tgp_probe_init(void)
{
	if (!dmi_first_match(px13_dmi_table)) {
		pr_err("px13-tgp: this module only supports ASUS HN7306WV\n");
		return -ENODEV;
	}

	px13_read_values();
	return 0;
}

static void __exit px13_tgp_probe_exit(void)
{
}

module_init(px13_tgp_probe_init);
module_exit(px13_tgp_probe_exit);

MODULE_DESCRIPTION("ASUS PX13 WMI power controller");
MODULE_AUTHOR("PX13 Linux Power contributors");
MODULE_LICENSE("GPL");
MODULE_VERSION("5.0");
MODULE_IMPORT_NS("ASUS_WMI");
